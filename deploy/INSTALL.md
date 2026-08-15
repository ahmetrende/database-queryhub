# Install (Linux + systemd)

> **Evaluating rather than deploying?** `docker compose up` at the repo root
> gives you the whole thing — app, metadata database, and a seeded demo target —
> in one command. It is explicitly not a deployment: fixed passwords, a
> throwaway key, plain HTTP. This document is for a real install.

> **Run ONE instance.** QueryHub is a single-process deployment today: the
> scheduler loop, the boot recovery of interrupted executions, and the local
> login throttle all live in the process. Query dispatch is `SKIP LOCKED`-safe,
> but boot recovery is not — a second replica sharing the same metadata database
> would re-run it, and the throttle would count per process instead of per user.
> There is no leader election yet.
>
> This is rarely the binding constraint, since approvals are human-paced. But it
> does mean **no active-active HA and no rolling deployment**: a restart is a
> short gap, not a hand-off. Put it behind a reverse proxy for TLS if you like;
> do not put two of them behind a load balancer. Plan for vertical scale and a
> fast restart rather than horizontal replicas.
>
> The restart itself is graceful — new submissions are refused, then in-flight
> executions are allowed to finish (bounded by `TimeoutStopSec`), so a deploy
> does not orphan a running query.
>
> Rest of the honest limits:
> [docs/KNOWN_LIMITATIONS.md](../docs/KNOWN_LIMITATIONS.md).


## Quick install (script)

The guided installer runs every step below in order, is idempotent
(re-run it any time — done steps skip), and prompts for all secrets
interactively (nothing sensitive on the command line):

```bash
bash scripts/install.sh                  # vanilla: web-only, local login
```

```bash
bash scripts/install.sh --slack --with-systemd   # + Slack surface + systemd units
```

Pass `--db-admin "host=… user=postgres dbname=postgres"` to let it run the
one-time superuser bootstrap too. The manual walk-through below remains
the reference for what each step does.

The bot runs as a normal Linux user out of a cloned repo. Pick a
location and a user; the rest follows.

```bash
# Set these once for the rest of this guide.
export REPO_DIR=/path/to/dba-slack-bot   # where you cloned the repo
export BOT_USER=$USER                    # the user the service runs as
```

`/etc/slackbot/{env,master.key,secrets.enc}` and the runtime data
directories below are owned by `$BOT_USER` (mode 0600 for files).

## 1. System packages

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip libpq-dev git
```

## 2. Service directories

```bash
sudo mkdir -p /etc/slackbot /var/lib/slackbot/results /var/log/slackbot
sudo chown -R "$BOT_USER:$BOT_USER" /etc/slackbot /var/lib/slackbot /var/log/slackbot
sudo chmod 700 /etc/slackbot
```

## 3. Virtualenv + Python deps

```bash
cd "$REPO_DIR"
python3.11 -m venv .venv
# Vanilla (web-only) profile:
.venv/bin/pip install -e .
# To run the Slack bot (/sql + Slack approval/notifications), add the extra:
.venv/bin/pip install -e '.[slack]'   # makes `python -m dba_slack_bot.main` resolvable
```

## 4. Generate the master key

The init script prints a fingerprint and refuses to write the key
until you type that fingerprint back, so you cannot skip the backup
step.

```bash
.venv/bin/python scripts/init_master_key.py /etc/slackbot/master.key
chmod 600 /etc/slackbot/master.key
```

> Without this file, every credential in the bot DB is unrecoverable.
> Back it up to AT LEAST TWO of: a password manager, an encrypted USB
> stick stored offline, a sealed envelope in a safe. Do NOT store it
> in git, Slack, email, or unencrypted shared drives.

A sidecar `/etc/slackbot/master.key.fingerprint` is written so you can
later verify the active key matches the one you backed up.

## 5. Bootstrap the bot's metadata DB

Run once from any host that can `psql` to the chosen Postgres
cluster as a superuser. Creates the `slackbot` database and the
`slackbot` login role. The password you choose here is what goes into
the encrypted secrets file in step 6.

```bash
sudo apt install -y postgresql-client    # if needed

read -s -p 'New slackbot password: ' BOT_DB_PASSWORD; echo
read -s -p 'Admin password: ' PGPASSWORD; echo; export PGPASSWORD

psql -h <dba-rds-host> -U <admin-user> -d postgres \
     -v bot_password="$BOT_DB_PASSWORD" \
     -f deploy/setup_db.sql

# Verify the slackbot role can log in:
PGPASSWORD="$BOT_DB_PASSWORD" psql -h <dba-rds-host> -U slackbot -d slackbot \
     -c 'select current_user, current_database();'

unset PGPASSWORD
```

The role is restricted: NOSUPERUSER, NOCREATEDB, NOCREATEROLE,
NOREPLICATION, connection-limit 20. Re-running the script with a new
`bot_password` rotates the password (idempotent).

Keep `$BOT_DB_PASSWORD` in your shell — you'll need it in the next step.

## 6. Environment file + encrypted secrets

The bot reads connection settings from `/etc/slackbot/env` (plaintext,
no secrets) and reads tokens + DB password from
`/etc/slackbot/secrets.enc` (Fernet-encrypted with the master key).

```bash
sudo cp "$REPO_DIR/.env.example" /etc/slackbot/env
sudo chown "$BOT_USER:$BOT_USER" /etc/slackbot/env
sudo chmod 600 /etc/slackbot/env
sudo nano /etc/slackbot/env   # fill BOT_DB_HOST / BOT_DB_NAME / BOT_DB_USER
```

> **Vanilla profile (no Slack):** leave the Slack token prompts blank — the
> web app runs without them and the Slack bot entrypoint stays disabled.
> Only the bot DB password is required.

Then store the secrets (Slack tokens + bot DB password) encrypted:

```bash
cd "$REPO_DIR"
sudo .venv/bin/python scripts/manage_env_secrets.py init
# Prompts (hidden, double-confirmed) for:
#   SLACK_BOT_TOKEN
#   SLACK_APP_TOKEN
#   BOT_DB_PASSWORD
```

This writes `/etc/slackbot/secrets.enc` (mode 0600). To rotate later:
`scripts/manage_env_secrets.py set <KEY>` then restart the service.

## 7. Apply migrations

```bash
set -a; source /etc/slackbot/env; set +a
export BOT_DB_PASSWORD="$BOT_DB_PASSWORD"   # still in your shell from step 5
.venv/bin/python scripts/apply_migrations.py
unset BOT_DB_PASSWORD
```

## 8. Add yourself as the first admin

Admin ops are raw SQL against the bot DB. Connect via psql / DataGrip
/ DBeaver as `slackbot:slackbot` and use the templates in
`deploy/db_admin_templates.sql`.

To add yourself (Slack profile → "..." → Copy member ID, looks like
`U01ABCDEFG`):

```sql
INSERT INTO admins (slack_user_id, name, added_by)
VALUES ('U01ABCDEFG', 'Your Name', 'system')
ON CONFLICT (slack_user_id) DO UPDATE
   SET name = EXCLUDED.name, enabled = TRUE
RETURNING slack_user_id, name, enabled, added_at;
```

**Vanilla profile (no Slack)** — there is no Slack member ID, so create a
built-in local account instead of the INSERT above. The password is read
interactively (never on the command line, stored only as a salted PBKDF2
hash — never cleartext); `--admin` makes it an unrestricted admin and turns
`web_auth_local_enabled` on:

```bash
.venv/bin/python scripts/create_local_user.py --username you --admin
```

You then log in at the web UI with that username and password. The
account's principal id is `local:you`; every later grant/admin op uses that
id in place of a Slack member id.

## 9. Add target servers

Target passwords are Fernet-encrypted before they hit the DB; the bot
decrypts at runtime with the master key. Encrypt the plaintext, then
INSERT via the template:

```bash
.venv/bin/python scripts/encrypt_secret.py
# Password: ********
# Re-enter: ********
# gAAAAABp...   ← copy this
```

Then run the "Add a target server" block in
`deploy/db_admin_templates.sql`, pasting the ciphertext as the
`password_encrypted` value. See `docs/OPERATIONS.md` section 5 for
the SQL.

## 10. Allow other users (teams + grants)

Admins bypass team auth, so while you are the only user there is
nothing to configure. When you onboard others, use the templates in
`deploy/team_admin_templates.sql`. See `docs/OPERATIONS.md` section 8.

Inspection views: `v_team_summary`, `v_user_targets`,
`v_effective_user_grants`.

## 11. Install and start the systemd service

The repo's `deploy/slackbot.service` file is a template with
`__USER__` and `__INSTALL_PATH__` placeholders. Substitute and copy:

```bash
sudo cp "$REPO_DIR/deploy/slackbot.service" /etc/systemd/system/slackbot.service
sudo sed -i "s|__USER__|$BOT_USER|g; s|__INSTALL_PATH__|$REPO_DIR|g" \
     /etc/systemd/system/slackbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now slackbot
sudo journalctl -u slackbot -f
```

## Web UI (required for the vanilla profile; optional alongside Slack)

The web surface is a separate process from the Slack bot — it shares the
same code, env file and bot DB, but runs on its own:

```bash
# behind TLS (Slack OIDC and cookie security both want https):
.venv/bin/python -m dba_slack_bot.web        # serves on :8080
```

Run it as its own systemd unit by copying `deploy/slackbot.service` to a
second unit (e.g. `queryhub-web.service`) and changing `ExecStart` to
`… -m dba_slack_bot.web`. Restarting the Slack bot does not restart the web
process and vice-versa.

**Frontend assets.** The web UI ships as source under `QueryHubWeb/`. For
production, build the optimized bundle once:

```bash
cd QueryHubWeb/app && npm install && npm run build   # -> QueryHubWeb/app/dist
```

The server serves `QueryHubWeb/app/dist` when present, and falls back to the
un-built single-file prototype (`QueryHubWeb/QueryHub.html`) otherwise — so a
fresh checkout works before you build, just unminified. The frontend lives
outside the Python package, so when you run from something other than a
source checkout (e.g. an installed wheel), point the server at the built
assets with the `QH_WEB_STATIC_DIR` env var:

```bash
QH_WEB_STATIC_DIR=/opt/queryhub/QueryHubWeb/app/dist .venv/bin/python -m dba_slack_bot.web
```

**Login.** With Slack configured, the sign-in screen offers Slack SSO
(set `web_auth_slack_enabled=on`, register the OIDC redirect). In the
vanilla profile it offers the username/password form for the local
accounts you created in step 8 (`web_auth_local_enabled=on`). Both can be
enabled at once; they are distinct principals.

**Approvals without Slack.** A DBA opens the web admin panel → **Review**
queue and clicks Approve / Reject. This runs the *same* decision core as a
Slack approval (`core_decide`), writes the same audit row, and — when Slack
is off — simply skips the Slack message mirror. Results and status are
served in the web UI; nothing is DM'd. So the full submit → approve →
execute → deliver loop works with no Slack workspace at all.

## 12. (Optional) Schedule CSV cleanup

Bot CSV results live for `results_ttl_hours` (default 48; set in
`bot_config`) on disk (`/var/lib/slackbot/results/`) and on Slack.
The cleanup helper deletes both and clears `requests.slack_file_id`.

Run on demand:

```bash
.venv/bin/python scripts/cleanup_old_results.py
```

Schedule daily via your preferred mechanism — for example a systemd
timer:

```ini
# /etc/systemd/system/slackbot-cleanup.service
[Unit]
Description=QueryHub — old results cleanup
After=network-online.target

[Service]
Type=oneshot
User=__USER__
WorkingDirectory=__INSTALL_PATH__
ExecStart=__INSTALL_PATH__/.venv/bin/python scripts/cleanup_old_results.py
EnvironmentFile=/etc/slackbot/env
```

```ini
# /etc/systemd/system/slackbot-cleanup.timer
[Unit]
Description=Run slackbot-cleanup daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo sed -i "s|__USER__|$BOT_USER|g; s|__INSTALL_PATH__|$REPO_DIR|g" \
     /etc/systemd/system/slackbot-cleanup.{service,timer}
sudo systemctl daemon-reload
sudo systemctl enable --now slackbot-cleanup.timer
```

A bare cron entry works just as well.

## Updating the bot

```bash
cd "$REPO_DIR"
git pull
.venv/bin/pip install -r requirements.txt    # only if requirements changed
.venv/bin/python scripts/apply_migrations.py # only if new migrations
sudo systemctl restart slackbot
```

## Migrating to a new host

All long-lived secrets are recoverable from the bot DB and the master
key file. To move:

1. Repeat steps 1, 2, 3, 6 (env file only — keep the password from
   the original DB), 11 on the new host.
2. Copy `/etc/slackbot/master.key` and `/etc/slackbot/secrets.enc`
   from the old host (preserve mode 0600, owner `$BOT_USER`). Verify
   the fingerprint matches `/etc/slackbot/master.key.fingerprint`.
3. Point `BOT_DB_HOST` etc. in `/etc/slackbot/env` at the same RDS DB.
4. Start the service. All target servers are immediately usable.
