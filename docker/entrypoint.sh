#!/bin/sh
# Container start-up: make the process able to run before running it.
#
# Order matters and each step is idempotent, because a container restarts.
#   1. master key      — generated once into the mounted volume, mode 0600
#   2. wait for the DB — compose's depends_on only waits for the container
#   3. migrations      — the ledger makes re-application a no-op
#   4. demo bootstrap  — only when QH_DEMO=1
#   5. first admin     — only when QH_ADMIN_USER is set (real installs)
set -eu

KEY="${MASTER_KEY_PATH:-/etc/queryhub/master.key}"
if [ ! -f "$KEY" ]; then
    echo "queryhub: generating master key at $KEY"
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > "$KEY"
    chmod 600 "$KEY"
fi
# crypto.py refuses a key readable by group or other, so fix an inherited mode
# rather than failing at the first decrypt.
chmod 600 "$KEY" 2>/dev/null || true

echo "queryhub: waiting for ${BOT_DB_HOST:-db}:${BOT_DB_PORT:-5432}"
i=0
until python - <<'PY'
import os, socket, sys
host = os.environ.get("BOT_DB_HOST", "db")
port = int(os.environ.get("BOT_DB_PORT", "5432"))
try:
    with socket.create_connection((host, port), timeout=2):
        sys.exit(0)
except OSError:
    sys.exit(1)
PY
do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
        echo "queryhub: database never became reachable" >&2
        exit 1
    fi
    sleep 1
done

echo "queryhub: applying migrations"
python scripts/apply_migrations.py

if [ "${QH_DEMO:-}" = "1" ]; then
    echo "queryhub: seeding demo data"
    python scripts/demo_bootstrap.py
    if [ -n "${QH_DEMO_ADMIN_PASSWORD:-}" ]; then
        python scripts/create_local_user.py \
            --username "${QH_DEMO_ADMIN_USER:-demo-admin}" --admin \
            --password-stdin <<PW || true
${QH_DEMO_ADMIN_PASSWORD}
PW
        python scripts/create_local_user.py \
            --username "${QH_DEMO_DEV_USER:-demo-dev}" \
            --password-stdin <<PW || true
${QH_DEMO_ADMIN_PASSWORD}
PW
    fi
    cat <<'BANNER'

  ==========================================================================
   QueryHub DEMO MODE
   Sign in at  http://localhost:8080
     admin      demo-admin  /  <QH_DEMO_ADMIN_PASSWORD>
     developer  demo-dev    /  <QH_DEMO_ADMIN_PASSWORD>

   DEMO ONLY. Fixed passwords, a throwaway target database, and demo
   credentials committed in plain sight. Never point this at production and
   never expose it beyond localhost.
  ==========================================================================

BANNER
fi

# The first admin account of a REAL install. Without it a fresh install has no
# way in at all: the web UI needs an account, and creating one meant getting a
# shell into the container — which is the step that made "install" mean "read
# the runbook". Everything after this point is doable in the UI (add a
# connection, test it, create a team, grant it), so this is the last thing that
# needs a terminal.
#
# Created ONCE and never touched again: a container restarts, and re-running the
# unconditional form would silently reset a password the operator had changed.
# Whatever password is used here is a BOOTSTRAP password — the account always
# carries must_change_pw, so the value sitting in .env stops being a working
# credential the moment somebody logs in.
if [ -n "${QH_ADMIN_USER:-}" ]; then
    if python - <<'PY'
import os, sys
from dba_slack_bot import local_users
sys.exit(0 if local_users.exists(
    local_users.normalize_username(os.environ["QH_ADMIN_USER"])) else 1)
PY
    then
        echo "queryhub: admin '${QH_ADMIN_USER}' already exists, leaving it alone"
    else
        generated=""
        if [ -z "${QH_ADMIN_PASSWORD:-}" ]; then
            # No password configured: mint one and print it once, rather than
            # inventing a default that would be the same on every install.
            QH_ADMIN_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(18))')"
            generated=1
        fi
        # A failure here is fatal on purpose (set -e): starting an install whose
        # admin account does not exist looks healthy and cannot be logged into.
        # The most likely cause is a password under 8 characters, and the script
        # says so.
        python scripts/create_local_user.py \
            --username "$QH_ADMIN_USER" --admin --must-change-pw --if-absent \
            --password-stdin <<PW
${QH_ADMIN_PASSWORD}
PW
        if [ -n "$generated" ]; then
            echo
            echo "  ======================================================================"
            echo "   QueryHub — first admin account created"
            echo "     username  ${QH_ADMIN_USER}"
            echo "     password  ${QH_ADMIN_PASSWORD}"
            echo
            echo "   This password is shown ONCE and is only good for one login: you"
            echo "   will be asked to set a new one. It is in this container's logs —"
            echo "   set QH_ADMIN_PASSWORD in .env if you would rather it never was."
            echo "  ======================================================================"
            echo
        fi
    fi
fi

exec "$@"
