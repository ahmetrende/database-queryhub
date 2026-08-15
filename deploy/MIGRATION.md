# QueryHub — host migration runbook

Moving the bot from one Linux host to another (e.g. an EC2 rebuild or a
new VM). This is a **host move only** — the metadata database, the
target clusters, and the Slack app stay exactly where they are.

> All paths, IPs, IAM roles, bucket names, and hostnames below are
> placeholders. Substitute the real values from the source host's
> `/etc/slackbot/*` and your infrastructure inventory. Never commit the
> real values.

## Why this is a light move

Two design choices make it easy:

1. **Socket Mode** — the bot has no public endpoint, DNS record, or
   load balancer. It opens an *outbound* WebSocket to Slack. The new
   host's IP can differ freely; nothing changes on the Slack side
   (no token reinstall, no URL update).
2. **The metadata DB does not move** — it lives on a managed Postgres
   instance the bot only connects to. No dump/restore, no migration
   replay; all state and applied migrations stay in place.

What actually moves: the code, a handful of secret files, and a systemd
unit. The real work is **network access**, not the application.

## What the target-cluster roles are NOT

The `dba_slackbot_ro/rw/ddl` login roles live *inside each target
cluster*, not on the bot host. A host move does not touch them — they
keep working, and the new host connects with the same (encrypted)
credentials. You only ever create these roles by hand when a **new
target cluster** is added to the inventory (`deploy/grant_readonly.sql`
et al.), which is unrelated to moving the bot host.

## Dependency inventory

| Component | State | Action on the new host |
|---|---|---|
| Code (`queryhub` repo) | in Git | `git clone` |
| `/etc/slackbot/master.key` | **most critical** | copy; every credential + Slack token is encrypted with it |
| `/etc/slackbot/master.key.fingerprint` | verification sidecar | copy |
| `/etc/slackbot/secrets.enc` | Slack tokens (Fernet-encrypted) | copy |
| `/etc/slackbot/env` | DB connection + key path | copy |
| `/etc/slackbot/dashboard.env` | dashboard S3 config | copy |
| Metadata DB | on managed Postgres, not moved | network access only |
| Slack tokens | inside `secrets.enc`, Socket Mode | unchanged, no reinstall |
| Python venv | rebuilt, not copied | `python3 -m venv` + `pip install -e .` |
| `<RESULTS_DIR>` (CSV results) | transient, short TTL | create the dir; no copy needed |
| `<LOG_DIR>` | logs | create the dir |
| systemd unit | template in repo | substitute + install |
| Target clusters | in place | add the new host IP to their network allow-list |
| IAM instance profile | grants S3 + DB access | attach the same profile to the new host |
| Scheduler jobs (dashboard publish, result cleanup) | host-local | move the job runner to the new host |

## The three things that, if missed, stop the bot

1. **`master.key`** — if it isn't copied, `secrets.enc` and every target
   credential can't be decrypted and the bot won't start. After copying,
   verify: the first 16 chars of `sha256sum master.key` must equal the
   `.fingerprint` file's contents.
2. **Network allow-list** — the new host's private IP must be added to
   the security group / firewall of **every** Postgres instance the bot
   reaches (the metadata DB + all target clusters). Missed entries mean
   the bot can't connect. This has the longest lead time — request it
   from the network team early.
3. **IAM instance profile** — the new host needs the same instance
   profile (or an equivalent one granting S3 write for the dashboard and
   any DB auth the current one provides).

## Phases

### Phase 0 — Prep (parallel, longest lead time)
- [ ] Provision the new host (Ubuntu, Python 3.11+ available)
- [ ] Note the new host's private IP
- [ ] Request: add the new IP to the security group of every target
      cluster + the metadata DB
- [ ] Attach the IAM instance profile to the new host
- [ ] Confirm outbound reachability to Slack (443) and the DBs (5432)

### Phase 1 — Host prep
- [ ] `apt install python3-venv libpq-dev git` (match the source host's
      Python minor version)
- [ ] Create `<RESULTS_DIR>` and `<LOG_DIR>`, owned by the run user
- [ ] Create `/etc/slackbot` (mode 700)

### Phase 2 — Code + secrets + venv
- [ ] `git clone` the repo into `<REPO_DIR>`
- [ ] Securely copy the five secret files into `/etc/slackbot`
      (env, master.key, master.key.fingerprint, secrets.enc,
      dashboard.env) — each mode 600, correct owner
- [ ] Verify the key fingerprint matches `master.key.fingerprint`
- [ ] Build the venv: `python3 -m venv .venv && .venv/bin/pip install -e '.[slack]'`
      (this host runs the Slack bot; drop `[slack]` for a web-only install)

### Phase 3 — Offline smoke test (safe while the old host still runs)
Test everything that does NOT open a Slack connection:
```bash
source .venv/bin/activate && set -a && source /etc/slackbot/env && set +a
python3 -c "from dba_slack_bot import db; db.init_pool(); print('DB OK')"
python3 scripts/check_repo_clean.py   # exercises DB access end to end
```
- [ ] DB connects, secrets decrypt
- [ ] **Do NOT start the full bot yet** — see the dual-instance warning

### Phase 4 — Cutover (brief downtime)
- [ ] Old host: `sudo systemctl stop slackbot && sudo systemctl disable slackbot`
      (a slow SIGTERM → SIGKILL leaves the unit in `failed`; that's normal —
      `systemctl reset-failed slackbot` tidies it for a clean standby)
- [ ] New host: install + **substitute the placeholders** in the unit
      template — it ships with `__USER__` / `__INSTALL_PATH__`, NOT real
      paths (`systemctl cat` on the *old* host shows the already-substituted
      copy, which is misleading). From the repo dir:
      ```
      sudo cp deploy/slackbot.service /etc/systemd/system/slackbot.service
      sudo sed -i "s|__USER__|$(whoami)|g; s|__INSTALL_PATH__|$(pwd)|g" \
           /etc/systemd/system/slackbot.service
      sudo systemd-analyze verify /etc/systemd/system/slackbot.service   # catches a missed sub
      sudo systemctl daemon-reload && sudo systemctl enable --now slackbot
      ```
- [ ] `journalctl -u slackbot` shows `Starting QueryHub` then
      `Bolt app is running!`

### Phase 5 — Verification
- [ ] `/sql whoami` responds in Slack
- [ ] Submit a read-only query → approve → result DM arrives (end to end)
- [ ] `bash scripts/publish_metrics_dashboard.sh`, then verify the
      published object with a `curl -sI` against its URL

> **Dashboard / IAM gotcha (learned the hard way).** The bot itself never
> touches AWS — only the dashboard publish (`aws s3 cp`) does. Two traps:
> 1. **SSO `[default]` profile shadows the instance role.** If the host has
>    a `~/.aws/config` with an SSO `[default]` profile (common on a DBA
>    workstation-style box), `aws` uses it and dies on an expired SSO token
>    instead of falling through to the EC2 instance role. The publish job
>    must bypass it — run with `AWS_CONFIG_FILE=/dev/null` (and
>    `AWS_SHARED_CREDENTIALS_FILE=/dev/null`) so the instance role is used.
> 2. **The instance role must have `s3:PutObject` on the dashboard bucket.**
>    A *differently-named* role (e.g. a new account's `*-ec2-db-admin`) is
>    not enough — if the bucket lives in another account, you need
>    cross-account write granted by DevOps, or repoint
>    `METRICS_DASHBOARD_BUCKET` to a bucket the new role owns. Verify BEFORE
>    cutover with a throwaway probe:
>    `aws s3 cp /tmp/x s3://$BUCKET/<prefix>/_probe && aws s3 rm ...`.
>    If it can't be granted in time, leave the publish job on the old host
>    (it keeps working — the dashboard reads the shared metadata DB, so it
>    stays accurate regardless of where the bot runs).

### Phase 6 — Cleanup

**If decommissioning the old host:**
- [ ] Move the scheduler jobs (periodic dashboard publish + daily result
      cleanup) to the new host — but only once the dashboard IAM/S3 access
      works there (see the Phase 5 gotcha; until then the publish job stays
      on the old host)
- [ ] Remove the old host's IP from the security groups
- [ ] Securely wipe the old host's `/etc/slackbot` secrets (`shred`)

**If keeping the old host as a hot standby (recommended for the first
while):** skip all three above. Leave its secrets, its security-group
entries, and its (disabled) `slackbot.service` in place so rollback is a
single `systemctl start slackbot`. The old host's job timers keep running —
in particular the dashboard publish keeps working there even after the bot
moves, since it only reads the shared metadata DB. The only caveat: don't
*also* enable a dashboard publish on the new host, or both would upload to
the same key every cycle.

## Dual-instance warning (Socket Mode)

If two bot processes connect to Slack with the same app token at the
same time, Slack splits events between them — a submission can be
processed twice and DMs get inconsistent. Therefore:
- In Phase 3, do **not** launch the full bot; test only the
  non-Slack components.
- The cutover order is strict: **stop the old, then start the new.**
  Accept a few minutes of downtime rather than overlap.

## Rollback

Nothing here is irreversible:
- Problem on the new host → stop it, re-start the bot on the old host
  (the old host is only stopped in Phase 4, not destroyed).
- The metadata DB never changed (shared), so both hosts see the same
  state.
- Only condition: keep the old host's IP in the security groups until
  the new host is fully verified — do the Phase 6 allow-list cleanup
  **after** verification, not before.

## Downtime estimate

Roughly 2–5 minutes (old stop → new start → first Socket connection).
In-flight approvals are unaffected — their state is in the DB, not in
the process.
