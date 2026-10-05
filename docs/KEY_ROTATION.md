# Rotating the master key

Follow this procedure to replace the master key with no downtime. The data
stays readable at every step.

The master key encrypts every target database credential. If you use the
env-secrets file, the key encrypts that file too.

Rotate the key when one of these conditions is true:

- The key may have been exposed.
- A person with filesystem access leaves.
- Your policy requires rotation on a schedule.

The key does not expire automatically. QueryHub will not remind you.

## What is encrypted

| Where | What |
|---|---|
| `target_servers.password_encrypted` | the RO credential per target |
| `target_servers.password_rw_encrypted` | the RW credential |
| `target_servers.password_ddl_encrypted` | the DDL credential |
| `$SECRETS_ENC_PATH` (optional) | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `BOT_DB_PASSWORD` |

Not affected: **local account passwords**. They are PBKDF2 hashes, not
ciphertext. Users do not need to reset anything.

Some targets get their credentials from an external secrets provider, for
example AWS Secrets Manager. For these targets, `secrets_provider` is set.
They store nothing locally to re-encrypt. Their columns are empty, so the
script skips them.

## The key file is a ring

`master.key` holds **one key per line, primary first**. QueryHub ignores blank
lines and `#` comments, so use comments to label the keys:

```
# rotated 2026-07-25 — delete the line below once step 5 is done
<new key>
<old key>
```

QueryHub always writes new ciphertext with the key on **line 1**. To decrypt,
it tries **every** line. This makes the transition safe: while both keys are
present, old and new ciphertext both work.

Keep the file at `chmod 600`. QueryHub refuses to start if the file is more
permissive.

## Procedure

**Save a copy of the current key file first.** While you still have the old
key, you can recover from every step below. After you lose the old key, you
cannot recover from any step.

### 1. Generate a key

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

### 2. Prepend it, keeping the old line

```
<the new key>
<the existing key>
```

### 3. Restart both services

```bash
sudo systemctl restart queryhub queryhub-web
```

From this point, QueryHub encrypts all new values with the new key. Values
that are already stored still decrypt with the old key.

1. Verify that the bot started.
2. Run one real query.

If this step is wrong, you want to know before step 4.

### 4. Re-encrypt what is already stored

```bash
set -a; source /etc/queryhub/env; set +a
.venv/bin/python scripts/rotate_master_key.py            # dry run first
.venv/bin/python scripts/rotate_master_key.py --apply
```

The script:

- is a **dry run** unless you pass `--apply`.
- decrypts, re-encrypts, then decrypts **again** and compares the result with
  the original before it commits. A value that does not round-trip aborts the
  whole run.
- does the database work in **one transaction**. Either every target moves,
  or none does.
- **skips values already on the primary key**, so you can repeat an
  interrupted run.
- **refuses to run with only one key on the ring**. With no old key, there is
  nothing to rotate from. The likely mistake is that you did step 5 early.

The script also writes:

- the secrets file, after the same round-trip test. It writes a temporary
  file, then renames it.
- an `audit_log` row (`master_key_rotated`).

Pass `--skip-secrets-file` to rotate the database only.

### 5. Verify, then drop the old key

1. Run a real query against a target.
2. Delete every line after line 1.
3. Restart again:

```bash
sudo systemctl restart queryhub queryhub-web
```

Until you do this, the old key still works. There is no deadline. Store the
retired key with your backups. Keep it for as long as you keep database
backups that were encrypted under it.

## If something goes wrong

**"none of the N key(s) … can read this ciphertext"**: a value is encrypted
with a key that is no longer in the file.

1. Add the old key again as a second line.
2. Restart.
3. Run step 4.

This is why step 5 comes last.

**"Line N of … is not a valid Fernet key"**: that line is malformed. A key is
44 characters of url-safe base64. The usual cause is a copy-paste that dropped
the trailing `=`.

**A restored database backup will not decrypt**: it was encrypted under an
older key.

1. Add that key to the ring as a second line.
2. Start QueryHub.
3. Run step 4.
4. Delete that key from the ring again.

Keep retired keys as long as you keep the backups.

**Interrupted step 4**: if the transaction did not complete, the script
committed nothing. The script is resumable. Run the dry run again to see what
is left.

## What this does not protect against

The same boundary applies as in the rest of `crypto.py`. An attacker who can
read *both* the ciphertext and `master.key` has both halves.

Rotation limits the value of a key that leaked on its own, for example:

- a stolen backup
- a mis-scoped file permission
- an old disk image

Rotation is not a defence against a live host compromise.
