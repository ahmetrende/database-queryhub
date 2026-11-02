#!/bin/sh
# Container start-up: make the process able to run before running it.
#
# Order matters and each step is idempotent, because a container restarts.
#   1. master key      — copied from QH_MASTER_KEY_SOURCE, or generated once
#                        into the mounted volume, mode 0600
#   2. wait for the DB — compose's depends_on only waits for the container
#   3. migrations      — the ledger makes re-application a no-op; skipped
#                        when QH_SKIP_MIGRATIONS=1
#   4. demo bootstrap  — only when QH_DEMO=1
#   5. first admin     — only when QH_ADMIN_USER is set (real installs)
#
# The command `init` runs these steps and exits 0. Kubernetes runs it as a
# Job before the app containers start (deploy/helm/queryhub).
set -eu

KEY="${MASTER_KEY_PATH:-/etc/queryhub/master.key}"
if [ -n "${QH_MASTER_KEY_SOURCE:-}" ]; then
    # A Kubernetes Secret mounts read-only, and with an fsGroup its file is
    # group-readable. crypto.py refuses a key that group or other can read, so
    # copy it to MASTER_KEY_PATH with mode 0600 (an in-memory volume there).
    if [ ! -r "$QH_MASTER_KEY_SOURCE" ]; then
        echo "queryhub: QH_MASTER_KEY_SOURCE=$QH_MASTER_KEY_SOURCE is not readable" >&2
        exit 1
    fi
    # The containers of one pod share this file. Write a temporary file and
    # rename it, so that a container that restarts never shows a running one
    # an empty or half-written key. An identical key stays as it is.
    if ! cmp -s "$QH_MASTER_KEY_SOURCE" "$KEY" 2>/dev/null; then
        ( umask 077 && cat "$QH_MASTER_KEY_SOURCE" > "$KEY.tmp.$$" && mv -f "$KEY.tmp.$$" "$KEY" )
        echo "queryhub: master key copied from QH_MASTER_KEY_SOURCE to $KEY"
    fi
elif [ ! -f "$KEY" ]; then
    # A generated key is right for a first evaluation and wrong for a real
    # install: every credential that it encrypts is lost with the volume. Set
    # QH_MASTER_KEY_AUTOGEN=0 to make a missing key stop the start instead.
    if [ "${QH_MASTER_KEY_AUTOGEN:-1}" = "0" ]; then
        echo "queryhub: no master key at $KEY and QH_MASTER_KEY_AUTOGEN=0" >&2
        exit 1
    fi
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

if [ "${QH_SKIP_MIGRATIONS:-0}" = "1" ]; then
    # A separate step applies them (the Kubernetes init Job). An app
    # container then never holds the migrator login.
    echo "queryhub: QH_SKIP_MIGRATIONS=1, migrations are not applied here"
else
    echo "queryhub: applying migrations"
    python scripts/apply_migrations.py
fi
# With split metadata roles the migrations ran as the migrator
# (BOT_DB_MIGRATOR_*). The service must not hold that login, so it leaves the
# environment here, before anything else runs. Running migrations as a separate
# one-off container keeps it out of this one altogether, which is better still.
unset BOT_DB_MIGRATOR_USER BOT_DB_MIGRATOR_PASSWORD BOT_DB_OWNER_ROLE

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
from queryhub import local_users
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

if [ "${1:-}" = "init" ]; then
    echo "queryhub: init done"
    exit 0
fi

# A scheduled job can start with a new image before the init step has applied
# that image's migrations. The new code then reads columns that do not exist
# yet. With QH_SKIP_IF_MIGRATIONS_PENDING=1 such a run exits 0 and does
# nothing, and the next scheduled run does the work. An unknown answer (no
# ledger, no access to it) lets the run go ahead.
if [ "${QH_SKIP_IF_MIGRATIONS_PENDING:-0}" = "1" ]; then
    set +e
    python - <<'PY'
import sys
from queryhub import db, migration_state
# migration_state reads only an open pool. A database that is down gives
# "unknown", and the run then reports that failure itself.
try:
    db.init_pool()
except Exception:
    sys.exit(0)
names = migration_state.pending(migration_state.shipped_versions())
sys.exit(3 if names else 0)
PY
    rc=$?
    set -e
    if [ "$rc" = "3" ]; then
        echo "queryhub: migrations are pending, this run does nothing"
        exit 0
    fi
    if [ "$rc" != "0" ]; then
        echo "queryhub: the migration check failed (exit $rc)" >&2
        exit "$rc"
    fi
fi

exec "$@"
