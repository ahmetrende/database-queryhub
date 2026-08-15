#!/usr/bin/env bash
# QueryHub guided installer — orchestrates the pieces deploy/INSTALL.md walks
# through by hand. Idempotent: every step detects "already done" and skips,
# so re-running after a failure (or for an upgrade) is safe.
#
#   bash scripts/install.sh                 # vanilla profile: web-only, local login
#   bash scripts/install.sh --slack         # + the Slack bot surface
#   bash scripts/install.sh --with-systemd  # also install + start systemd unit(s)
#   bash scripts/install.sh --db-admin "host=... user=postgres dbname=postgres"
#                                           # let the script run the one-time DB
#                                           # bootstrap via psql as a superuser
#
# Secrets are always typed interactively (hidden prompts) — never passed on
# the command line, never stored in shell history, never written cleartext:
# the DB password + optional Slack tokens land Fernet-encrypted in
# secrets.enc, and local-account passwords are stored only as salted PBKDF2
# hashes. Run as a normal user; sudo is invoked only for /etc paths and
# systemd. Config dir defaults to /etc/slackbot (override: QH_CONF_DIR).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF_DIR="${QH_CONF_DIR:-/etc/slackbot}"
VENV="$REPO_DIR/.venv"
PYTHON="${PYTHON:-python3}"
WITH_SLACK=0
WITH_SYSTEMD=0
DB_ADMIN_CONN=""

while [ $# -gt 0 ]; do
  case "$1" in
    --slack) WITH_SLACK=1 ;;
    --with-systemd) WITH_SYSTEMD=1 ;;
    --db-admin) DB_ADMIN_CONN="${2:?--db-admin needs a psql conninfo string}"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,18p'; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

step()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()    { printf '   \033[32m%s\033[0m\n' "$*"; }
skip()  { printf '   [skip] %s\n' "$*"; }
note()  { printf '   %s\n' "$*"; }
warn()  { printf '   \033[33m! %s\033[0m\n' "$*"; }

[ "$(id -u)" = 0 ] && { echo "Run as a normal user, not root (sudo is used where needed)."; exit 2; }

# ---- 1. Python ---------------------------------------------------------------
step "1/10 Python"
"$PYTHON" - <<'PY' || { echo "   Python 3.11+ required."; exit 2; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
ok "$($PYTHON --version)"

# ---- 2. Virtualenv + package ---------------------------------------------------
step "2/10 Virtualenv + package"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
  ok "created $VENV"
else
  skip "venv exists"
fi
"$VENV/bin/pip" -q install --upgrade pip
if [ "$WITH_SLACK" = 1 ]; then
  "$VENV/bin/pip" -q install -e "$REPO_DIR[slack]" && ok "installed package + [slack] extra"
else
  "$VENV/bin/pip" -q install -e "$REPO_DIR" && ok "installed package (vanilla profile — no Slack SDK)"
fi

# ---- 3. Config dir -------------------------------------------------------------
step "3/10 Config dir ($CONF_DIR)"
if [ ! -d "$CONF_DIR" ]; then
  sudo mkdir -p "$CONF_DIR"
  sudo chown "$(id -un):$(id -gn)" "$CONF_DIR"
  sudo chmod 700 "$CONF_DIR"
  ok "created"
else
  skip "exists"
fi

# ---- 4. Master key -------------------------------------------------------------
step "4/10 Master key"
if [ ! -f "$CONF_DIR/master.key" ]; then
  "$VENV/bin/python" "$REPO_DIR/scripts/init_master_key.py" "$CONF_DIR/master.key"
  note "BACK THIS FILE UP — it decrypts every stored credential."
else
  skip "master.key exists"
fi

# ---- 5. Env file ---------------------------------------------------------------
step "5/10 Env file ($CONF_DIR/env)"
if [ ! -f "$CONF_DIR/env" ]; then
  read -rp "   Bot metadata DB host: " DB_HOST
  read -rp "   Bot metadata DB port [5432]: " DB_PORT; DB_PORT="${DB_PORT:-5432}"
  read -rp "   Bot metadata DB name [slackbot]: " DB_NAME; DB_NAME="${DB_NAME:-slackbot}"
  read -rp "   Bot metadata DB user [slackbot]: " DB_USER; DB_USER="${DB_USER:-slackbot}"
  umask 177
  cat > "$CONF_DIR/env" <<EOF
# Written by scripts/install.sh — non-secret runtime configuration.
# Secrets live Fernet-encrypted in secrets.enc (manage_env_secrets.py).
BOT_DB_HOST=$DB_HOST
BOT_DB_PORT=$DB_PORT
BOT_DB_NAME=$DB_NAME
BOT_DB_USER=$DB_USER
MASTER_KEY_PATH=$CONF_DIR/master.key
LOG_LEVEL=INFO
EOF
  umask 022
  ok "written (mode 600)"
else
  skip "env exists"
fi
set -a; . "$CONF_DIR/env"; set +a

# ---- 6. Encrypted secrets -------------------------------------------------------
step "6/10 Encrypted secrets ($CONF_DIR/secrets.enc)"
if [ ! -f "${SECRETS_ENC_PATH:-$CONF_DIR/secrets.enc}" ]; then
  if [ "$WITH_SLACK" = 1 ]; then
    note "You'll be prompted for SLACK_BOT_TOKEN / SLACK_APP_TOKEN / BOT_DB_PASSWORD."
  else
    note "Vanilla profile: leave both Slack token prompts EMPTY; only the DB password matters."
  fi
  "$VENV/bin/python" "$REPO_DIR/scripts/manage_env_secrets.py" init
else
  skip "secrets.enc exists"
fi

# ---- 7. Metadata DB bootstrap + migrations --------------------------------------
step "7/10 Metadata DB"
if [ -n "$DB_ADMIN_CONN" ]; then
  note "Running one-time bootstrap (deploy/setup_db.sql) as the given admin…"
  read -rsp "   Password for the '$BOT_DB_USER' role (will be stored encrypted): " BOTPW; echo
  psql "$DB_ADMIN_CONN" -v bot_password="$BOTPW" -f "$REPO_DIR/deploy/setup_db.sql"
  unset BOTPW
  ok "bootstrap done"
else
  note "Skipping the one-time superuser bootstrap (no --db-admin)."
  note "If the '$BOT_DB_NAME' DB / '$BOT_DB_USER' role don't exist yet, run:"
  note "  psql -h $BOT_DB_HOST -U <admin> -d postgres -v bot_password='…' -f deploy/setup_db.sql"
fi
if "$VENV/bin/python" "$REPO_DIR/scripts/apply_migrations.py" --dry-run >/dev/null 2>&1; then
  "$VENV/bin/python" "$REPO_DIR/scripts/apply_migrations.py"
  ok "migrations applied"
else
  echo "   ERROR: cannot reach the metadata DB with $CONF_DIR/env settings."
  echo "   Finish the bootstrap above, then re-run this script — it resumes here."
  exit 1
fi

# ---- 8. First admin (vanilla: local account) -------------------------------------
step "8/10 First admin"
if [ "$WITH_SLACK" = 1 ]; then
  note "Slack profile: add yourself by Slack member id (see deploy/INSTALL.md §8):"
  note "  INSERT INTO admins (slack_user_id, name, added_by) VALUES ('U…', 'You', 'install');"
else
  read -rp "   Create a local admin account now? [Y/n] " YN
  if [ "${YN:-Y}" != "n" ] && [ "${YN:-Y}" != "N" ]; then
    read -rp "   Username: " LU
    "$VENV/bin/python" "$REPO_DIR/scripts/create_local_user.py" --username "$LU" --admin
  else
    skip "create later: scripts/create_local_user.py --username you --admin"
  fi
fi

# ---- 9. Frontend build ------------------------------------------------------------
# The web UI ships as source and the server serves ONLY the built bundle —
# there is no CDN fallback, because the app's own CSP blocks external scripts.
# Skipping this step is what produced the "blank page after install" first-run
# failure; now it either builds here or the browser gets an explicit
# "frontend not built" page with this command in it.
step "9/10 Frontend build"
APP_DIR="$REPO_DIR/QueryHubWeb/app"
if [ -f "$APP_DIR/dist/index.html" ]; then
  skip "frontend already built ($APP_DIR/dist)"
elif command -v npm >/dev/null 2>&1; then
  if ( cd "$APP_DIR" && npm install --silent && npm run build >/dev/null 2>&1 ); then
    ok "frontend built -> $APP_DIR/dist"
  else
    warn "frontend build failed — the web UI will show a 'not built' page"
    echo "        Retry with: cd $APP_DIR && npm install && npm run build"
  fi
else
  warn "npm not found — the browser UI cannot be served yet (the API still works)"
  echo "        Install Node.js 18+, then: cd $APP_DIR && npm install && npm run build"
fi

# ---- 10. TLS + how to run ---------------------------------------------------------
step "10/10 Web TLS + run"
TLS_DIR="$CONF_DIR/web-tls"
if [ ! -f "$TLS_DIR/cert.pem" ]; then
  read -rp "   Generate a self-signed localhost TLS cert? [Y/n] " YN
  if [ "${YN:-Y}" != "n" ] && [ "${YN:-Y}" != "N" ]; then
    mkdir -p "$TLS_DIR"
    openssl req -x509 -newkey rsa:2048 -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" \
      -days 825 -nodes -subj "/CN=localhost" >/dev/null 2>&1
    chmod 600 "$TLS_DIR/key.pem"
    ok "self-signed cert at $TLS_DIR (browser warning is expected)"
  fi
else
  skip "cert exists"
fi

if [ "$WITH_SYSTEMD" = 1 ]; then
  UNIT=/etc/systemd/system/queryhub-web.service
  sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=QueryHub Web — FastAPI app + static frontend
After=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$REPO_DIR
EnvironmentFile=$CONF_DIR/env
Environment=WEB_SSL_CERTFILE=$TLS_DIR/cert.pem
Environment=WEB_SSL_KEYFILE=$TLS_DIR/key.pem
ExecStart=$VENV/bin/python -m dba_slack_bot.web
Restart=on-failure
RestartSec=5
# Graceful restart: on SIGTERM the app drains (refuses new submissions) and
# waits for in-flight query executions to finish, so a restart never orphans a
# running query. This ceiling must exceed the longest a query can run
# (bot_config.query_timeout_sec, default 300s); idle restarts still exit at
# once. Without it systemd SIGKILLs at the 90s default and the documented
# drain guarantee silently does not hold.
TimeoutStopSec=330s

# Hardening — matches deploy/slackbot.service. Runs as a normal user; /, /usr,
# /boot and /etc are read-only under ProtectSystem=strict, so only the runtime
# dirs are writable.
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/slackbot /var/log/slackbot
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now queryhub-web
  ok "queryhub-web.service running (https://localhost:8080)"
  if [ "$WITH_SLACK" = 1 ]; then
    note "For the Slack bot unit, see deploy/INSTALL.md §11 (deploy/slackbot.service)."
  fi
else
  echo
  echo "Done. Start the web app with:"
  echo "  set -a; . $CONF_DIR/env; set +a"
  echo "  WEB_SSL_CERTFILE=$TLS_DIR/cert.pem WEB_SSL_KEYFILE=$TLS_DIR/key.pem \\"
  echo "    $VENV/bin/python -m dba_slack_bot.web"
  echo "then open https://localhost:8080 and sign in."
  [ "$WITH_SLACK" = 1 ] && echo "Slack bot: $VENV/bin/python -m dba_slack_bot.main (see INSTALL.md §11 for systemd)."
fi
echo
echo "Next steps: add target servers (deploy/INSTALL.md §9) and grants (§10)."
