# Backup and recovery

QueryHub is one process, one metadata database and one encryption key.
The loss of each one has a different consequence, and only one of the
three losses is unrecoverable. This document says which one and how to
make backups of all three. More usefully, it shows how to prove that your
backup works before you need it.

This document is about recovery, not about staying up. There is no HA
story here: a single node is the supported topology (see
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)).

## 1. What to back up

There are three things. The most important comes first.

### The master key — `/etc/queryhub/master.key`

**Irreplaceable.** QueryHub uses this one key to Fernet-encrypt every
target credential in `target_servers` and every value in `secrets.enc`
(Slack tokens, the metadata DB password). Without the key, a perfectly
good database restore gives a gateway that lists every target and fails
every execution.

`scripts/init_master_key.py` already refuses to finish until you re-type
the key's fingerprint. It also prints the rule to keep backups in at
least two locations. `master.key` sits next to `master.key.fingerprint`,
so you can verify a restored copy without decrypting anything.

Keep copies in at least two of these places:

- a password manager
- an encrypted offline drive
- a sealed envelope in a safe

Do not put a copy on the same volume as the database. The loss of that
volume is the failure this document exists for.

The key is 44 bytes. There is no excuse.

**Irreplaceable is not the same as unchangeable.** You cannot recover a
key you lost, but you *can* replace one you still have. The file is a key
ring: a new key goes on line 1, and the old one keeps decrypting until
everything is re-encrypted. The full procedure is in
[KEY_ROTATION.md](KEY_ROTATION.md). Two consequences for backups:

- You can restore a database backup only with the key that was current
  **when it was taken**. Keep each retired key for as long as you keep
  backups encrypted under it, not only the newest key.
- If a restored backup fails to decrypt, the cause is almost certainly a
  key-generation mismatch, not corruption. To recover:
  1. Add the older key as a second line.
  2. Start the service.
  3. Re-encrypt.
  4. Delete the older key line.

### The metadata database

The metadata database holds everything else: targets, admins,
requesters, teams, grants, config, every request and the audit log. Use a
standard Postgres backup. It needs nothing special:

```bash
pg_dump -Fc -h "$BOT_DB_HOST" -U "$BOT_DB_USER" -d "$BOT_DB_NAME" \
        -f queryhub-$(date -u +%Y%m%dT%H%M%SZ).dump
```

Take it on the same schedule as any other production database. On a
managed service, you can instead make sure of two things:

- Automated backups and PITR are on.
- You know the retention window.

If you need to prioritise, these tables matter most:

- `target_servers` (credential ciphertext)
- `audit_log` and `requests` (the record of every decision)
- `local_users` (password hashes)
- `bot_config`

### The configuration directory — `/etc/queryhub/`

```
master.key              the key (above)
master.key.fingerprint  its sha256, for verifying a restore
secrets.enc             encrypted Slack tokens + DB password
env                     non-secret connection settings
web-tls/                the web TLS certificate and key
```

The directory is small and changes rarely, and it is the fastest way to a
running system. Make a backup of the whole directory. It is a few
kilobytes.

### What NOT to back up

Exclude `/var/lib/queryhub/results` (or wherever `QH_RESULTS_DIR` points)
from the backup. It holds the query result files:

- They hold the most sensitive data in the system.
- QueryHub deletes them automatically after `results_ttl_hours` (default
  72).
- A re-run of the query reproduces them.

A backup of these files quietly converts a dataset with a 72-hour TTL
into a permanent one. See
[COMPLIANCE.md](COMPLIANCE.md#2-what-is-stored-by-table).

## 2. Objectives, stated rather than implied

For a single-node install:

| | |
| --- | --- |
| **RPO** | your Postgres backup interval. Continuous archiving / PITR → seconds. A nightly `pg_dump` → up to 24 hours of requests and audit rows. Nothing in QueryHub buffers writes. Every state change commits with its audit row in the same transaction, so a restore is consistent to whatever instant you restore to. |
| **RTO** | roughly 15 minutes, and almost all of it is the database restore. The application itself is a `pip install` and a `systemctl start`. |
| **Data loss on key loss** | total, for stored credentials, but not for the audit trail. To recover, re-provision every target credential by hand (N targets × 3 tiers) and re-enter the Slack tokens. Budget days for a fleet, not minutes. |

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

A restore that starts is not a restore that works. Run these four tests.
Each one fails loudly if a different piece is wrong:

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

If (a) fails, stop. You restored a database with a key that does not
belong to it. Find the right key before you do anything else. You cannot
undo a re-provisioning of credentials.

## 4. Drill it

Do this drill once, then every year, and after any change to how you
store the key. It takes fifteen minutes. It is the only thing that turns
"we have backups" into a fact.

1. Start an empty Postgres. A container is fine: `docker compose up db`.
2. Restore yesterday's dump into it.
3. Point a *copy* of the config directory at it (`BOT_DB_HOST` in `env`).
   Never use the live one.
4. Run every test in section 3.
5. Record the wall-clock time it took. That number is your real RTO. The
   15 minutes above is an estimate, and yours is a measurement.

Restore into a throwaway database, never over the live one.

## 5. If you lose the master key

There is no recovery path, only a rebuild. Do these steps in order:

1. **Do not** run `init_master_key.py --force` against the live config
   before you read this list. It destroys the ability to decrypt anything
   currently stored. The script says so before it does it.
2. Generate a new key (`scripts/init_master_key.py`). Make a proper backup
   of it this time.
3. Re-encrypt the environment secrets. Run
   `scripts/manage_env_secrets.py init`, then `set` each of
   `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `BOT_DB_PASSWORD`.
4. For **every** target and **every** tier, re-enter the password. Use
   `scripts/encrypt_secret.py`, then update
   `target_servers.password_*_encrypted`. The credentials themselves still
   work. Only your copy of them is unreadable, so this is data entry, not
   a password reset across the fleet. If this was the only place that ever
   stored the passwords, it is both.
5. The audit trail and all history are untouched. You lose credentials,
   not the record.

Key **rotation** uses the key ring and `scripts/rotate_master_key.py`. The
service keeps running, and no window leaves the data unreadable.
KEY_ROTATION.md gives the procedure.

## 6. Losing only the database

This loss is less dramatic. It is worth a note, because the failure modes
are not symmetrical:

- **Credentials survive**: they are in the dump, and your key still
  decrypts them.
- **The audit trail does not.** Rows since the last backup are gone, and
  there is no reconstruction. The target databases hold the effect of an
  approved query, not the record of who approved it. This is the one loss
  that QueryHub cannot help you with. So this loss decides your backup
  interval.
- Result files for those requests may still be on disk (within the TTL),
  with no matching row. `scripts/cleanup_old_results.py` deletes orphans
  by age.

## 7. Where the remaining risk is

The risks, written down rather than left implied:

- **Single node.** There is no failover and no read replica of the control
  plane. A dead host means downtime until you replace it. Nothing
  executes, and no approval is lost: requests stay in the state they were
  in, and the next boot re-queues the `approved` rows.
- **Losing every key on the ring loses the data** (section 5).
- **Mutable audit log.** Anyone with direct database access can edit
  `audit_log`. QueryHub blocks granting *itself* access to its own
  control-plane database, so nobody can use it to tamper with its own
  record. But a DBA with psql is outside that boundary. If you need
  tamper-evidence, it is external to QueryHub. Ship the log to a WORM
  target, or wait for the immutable-audit roadmap item.
- **No schema-version gate at boot.** The app starts against an older
  schema and does not refuse. Step 3 above is the manual test that
  replaces the gate.
