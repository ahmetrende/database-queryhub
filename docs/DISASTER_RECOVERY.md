# Backup and recovery

QueryHub is one process, one metadata database, and one encryption key. Losing
any of the three has a different consequence, and only one of them is
unrecoverable. This document says which, how to back them up, and — more usefully
— how to prove your backup works before you need it.

There is no HA story here. A single node is the supported topology (see
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)); this is about recovery, not about
staying up.

## 1. What to back up

Three things, in order of how badly you want them.

### The master key — `/etc/queryhub/master.key`

**Irreplaceable.** Every target credential in `target_servers` and every value in
`secrets.enc` (Slack tokens, the metadata DB password) is Fernet-encrypted with
this one key. Without it, a perfectly good database restore produces a gateway
that lists every target and fails every execution.

`scripts/init_master_key.py` already refuses to finish until you type the key's
fingerprint back, and prints the two-locations-minimum backup rule; `master.key`
sits next to `master.key.fingerprint` so you can verify a restored copy without
decrypting anything. Back it up to at least two of: a password manager, an
encrypted offline drive, a sealed envelope in a safe. Not to the same volume as
the database, which is the failure this document exists for.

44 bytes. There is no excuse.

**Irreplaceable is not the same as unchangeable.** You cannot recover a key you
lost, but you *can* replace one you still have — the file is a key ring, so a
new key goes on line 1 while the old one keeps decrypting until everything has
been re-encrypted. Full procedure in [KEY_ROTATION.md](KEY_ROTATION.md). Two
consequences for backups:

- A database backup is only restorable with the key that was current **when it
  was taken**. Keep each retired key for as long as you keep backups encrypted
  under it, not just the newest one.
- If a restored backup fails to decrypt, it is almost certainly a key-generation
  mismatch rather than corruption. Add the older key as a second line, start,
  re-encrypt, then remove it.

### The metadata database

Holds everything else: targets, admins, requesters, teams, grants, config, every
request, and the audit log. Standard Postgres backup, nothing special:

```bash
pg_dump -Fc -h "$BOT_DB_HOST" -U "$BOT_DB_USER" -d "$BOT_DB_NAME" \
        -f queryhub-$(date -u +%Y%m%dT%H%M%SZ).dump
```

Take it on the same schedule as any other production database — or, on a managed
service, just make sure automated backups and PITR are on and that you know the
retention window. The crown jewels inside it, if you need to prioritise:
`target_servers` (credential ciphertext), `audit_log` and `requests` (the record
of every decision), `local_users` (password hashes), `bot_config`.

### The configuration directory — `/etc/queryhub/`

```
master.key              the key (above)
master.key.fingerprint  its sha256, for verifying a restore
secrets.enc             encrypted Slack tokens + DB password
env                     non-secret connection settings
web-tls/                the web TLS certificate and key
```

Small, changes rarely, and the fastest way to a running system. Back up the whole
directory; it is a few kilobytes.

### What NOT to back up

`/var/lib/queryhub/results` (or wherever `QH_RESULTS_DIR` points). Those are
query result files: they hold the most sensitive data in the system, they are
deleted automatically after `results_ttl_hours` (default 72), and they are
reproducible by re-running the query. Backing them up quietly converts a
72-hour-TTL dataset into a permanent one — see
[COMPLIANCE.md](COMPLIANCE.md#2-what-is-stored-by-table).

## 2. Objectives, stated rather than implied

For a single-node install:

| | |
| --- | --- |
| **RPO** | your Postgres backup interval. Continuous archiving / PITR → seconds. A nightly `pg_dump` → up to 24 hours of requests and audit rows. Nothing in QueryHub buffers writes: every state change commits with its audit row in the same transaction, so a restore is consistent to whatever instant you restore to. |
| **RTO** | roughly 15 minutes, and almost all of it is the database restore. The application itself is a `pip install` and a `systemctl start`. |
| **Data loss on key loss** | total, for stored credentials. Not for the audit trail. Recovery = re-provision every target credential by hand (N targets × 3 tiers) and re-enter the Slack tokens. Budget days for a fleet, not minutes. |

## 3. Recovery

```bash
# 1. Restore the configuration directory, then verify the key is the right one.
sudo install -m 700 -o queryhub -g queryhub -d /etc/queryhub
# ...restore master.key, secrets.enc, env, web-tls/ from your backup...
sudo chmod 600 /etc/queryhub/master.key        # crypto.py refuses a laxer mode
sha256sum /etc/queryhub/master.key | cut -c1-16
cat /etc/queryhub/master.key.fingerprint       # these must match

# 2. Restore the database.
createdb -h "$BOT_DB_HOST" -U postgres "$BOT_DB_NAME"
pg_restore -h "$BOT_DB_HOST" -U postgres -d "$BOT_DB_NAME" queryhub-....dump

# 3. Confirm the schema matches the code you are about to run. This changes
#    nothing; it prints the plan.
python scripts/apply_migrations.py --dry-run
#    Empty plan  -> schema and code agree.
#    Pending     -> the backup predates this code; apply them.

# 4. Start, and check readiness rather than assuming.
sudo systemctl start queryhub-web        # and queryhub (the Slack bot) if used
curl -fsS https://localhost:8080/readyz  # {"status":"ready"} means the pool is up
```

### Verify the restore actually works

A restore that starts is not a restore that works. Four checks, each of which
fails loudly if a different piece is wrong:

```bash
# (a) the key decrypts what is in the database — the check that catches a
#     mismatched key/DB pair, which is the most likely bad restore
python - <<'EOF'
from queryhub import targets
t = targets.list_all()[0]
user, password = targets.get_credentials(t.id, "ro")
print(f"decrypted the RO credential for {t.alias}: user={user} "
      f"password_len={len(password)}")
EOF

# (b) the audit trail came back
psql -c "SELECT count(*) AS audit_rows, max(created_at) AS newest FROM audit_log;"

# (c) the request history came back, and its newest row matches your RPO
psql -c "SELECT count(*) AS requests, max(created_at) AS newest FROM requests;"

# (d) nothing is stuck mid-execution. Rows left 'executing' by the crash are
#     reconciled at boot; this should be empty a minute after start.
psql -c "SELECT id, status, executed_at FROM requests
          WHERE status IN ('executing','approved') ORDER BY id;"
```

If (a) fails, stop: you have restored a database with a key that does not belong
to it. Find the right key before doing anything else — re-provisioning
credentials is a one-way door.

## 4. Drill it

Do this once, then annually, and after any change to how the key is stored. It
takes fifteen minutes and it is the only thing that turns "we have backups" into
a fact.

1. Bring up an empty Postgres (a container is fine — `docker compose up db`).
2. Restore yesterday's dump into it.
3. Point a *copy* of the config directory at it (`BOT_DB_HOST` in `env`) — never
   the live one.
4. Run every check in section 3.
5. Write down the wall-clock time it took. That number is your real RTO; the 15
   minutes above is an estimate, yours is a measurement.

Restore into a throwaway database, never over the live one.

## 5. If you lose the master key

There is no recovery path, only a rebuild. In order:

1. **Do not** run `init_master_key.py --force` against the live config until you
   have read this list — it destroys the ability to decrypt anything currently
   stored, and the script says so before it does it.
2. Generate a new key (`scripts/init_master_key.py`) and back it up properly this
   time.
3. Re-encrypt the environment secrets: `scripts/manage_env_secrets.py init`, then
   `set` each of `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `BOT_DB_PASSWORD`.
4. For **every** target and **every** tier, re-enter the password:
   `scripts/encrypt_secret.py` and update `target_servers.password_*_encrypted`.
   The credentials themselves still work — it is only your copy of them that is
   unreadable — so this is data entry, not a password reset across the fleet.
   Unless the passwords were only ever stored here, in which case it is both.
5. The audit trail and all history are untouched. You lose credentials, not the
   record.

Key **rotation** has the same shape and no tooling: there is one key, ciphertext
carries no key id, so rotating means re-encrypting everything in one pass with
the service stopped. Multi-key support (a `key_id` column and an active-key
setting) is on the roadmap precisely so this stops being a documented "don't".

## 6. Losing only the database

Less dramatic, and worth stating because the failure modes are not symmetrical:

- **Credentials survive** — they are in the dump, and your key still decrypts
  them.
- **The audit trail does not.** Rows since the last backup are gone, and there is
  no reconstruction: the target databases hold the effect of an approved query,
  not the record of who approved it. This is the one loss QueryHub cannot help
  you with, so it is the one to set your backup interval by.
- Result files for those requests may still be on disk (within the TTL) with no
  matching row. `scripts/cleanup_old_results.py` removes orphans by age.

## 7. Where the remaining risk is

Written down rather than left implied:

- **Single node.** No failover, no read replica of the control plane. A dead host
  is downtime until it is replaced; nothing executes and no approval is lost
  (requests stay in the state they were in, and `approved` rows are re-queued at
  next boot).
- **One key, no versioning** (section 5).
- **Mutable audit log.** Anyone with direct database access can edit
  `audit_log`. QueryHub blocks granting *itself* access to its own control-plane
  database, so it cannot be used to tamper with its own record — but a DBA with
  psql is outside that boundary. If you need tamper-evidence, that is external:
  ship the log to a WORM target, or wait for the immutable-audit roadmap item.
- **No schema-version gate at boot.** The app starts against an older schema
  rather than refusing; step 3 above is the manual check that replaces it.
