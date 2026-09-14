# Rotating the master key

The master key encrypts every target database credential and, if you use it,
the env-secrets file. This page is the procedure for replacing it without
downtime and without a window where the data is unreadable.

Rotate when: the key may have been exposed, someone with filesystem access
leaves, or your policy says so on a schedule. There is no automatic expiry —
QueryHub will not nag you.

## What is encrypted

| Where | What |
|---|---|
| `target_servers.password_encrypted` | the RO credential per target |
| `target_servers.password_rw_encrypted` | the RW credential |
| `target_servers.password_ddl_encrypted` | the DDL credential |
| `$SECRETS_ENC_PATH` (optional) | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `BOT_DB_PASSWORD` |

Not affected: **local account passwords**. Those are PBKDF2 hashes, not
ciphertext — users do not need to reset anything.

Targets whose credentials come from an external secrets provider
(`secrets_provider` set, e.g. AWS Secrets Manager) have nothing stored locally
to re-encrypt, and the script skips them because their columns are empty.

## The key file is a ring

`master.key` holds **one key per line, primary first**. Blank lines and `#`
comments are ignored, so label them:

```
# rotated 2026-07-25 — delete the line below once step 5 is done
<new key>
<old key>
```

New ciphertext is always written with **line 1**. Decryption tries **every**
line. That is what makes the transition safe: while both keys are present,
old and new ciphertext both work.

Keep the file `chmod 600`. QueryHub refuses to start if it is more permissive.

## Procedure

**Back up the current key file first.** Everything below is recoverable while
you still have the old key; nothing is once you have lost it.

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

From here, anything newly encrypted uses the new key. Everything already
stored still decrypts with the old one. Confirm the bot came up and run one
real query — if this step is wrong, you want to know before step 4.

### 4. Re-encrypt what is already stored

```bash
set -a; source /etc/queryhub/env; set +a
.venv/bin/python scripts/rotate_master_key.py            # dry run first
.venv/bin/python scripts/rotate_master_key.py --apply
```

The script:

- is a **dry run** unless you pass `--apply`;
- decrypts, re-encrypts, then decrypts **again** and compares to the original
  before committing — a value that does not round-trip aborts the whole run;
- does the database work in **one transaction**, so either every target moves
  or none does;
- **skips values already on the primary key**, so an interrupted run can simply
  be repeated;
- writes the secrets file via temp-and-rename after the same round-trip check;
- writes an `audit_log` row (`master_key_rotated`);
- **refuses to run with only one key on the ring** — with no old key there is
  nothing to rotate from, and the likely mistake is having done step 5 early.

`--skip-secrets-file` rotates the database only.

### 5. Verify, then drop the old key

Run a real query against a target. Then delete every line after line 1 and
restart again:

```bash
sudo systemctl restart queryhub queryhub-web
```

Until you do this, the old key still works — there is no deadline. Store the
retired key with your backups for as long as you keep database backups that
were encrypted under it.

## If something goes wrong

**"none of the N key(s) … can read this ciphertext"** — a value is encrypted
with a key that is no longer in the file. Put the old key back as a second
line, restart, and run step 4. This is why step 5 comes last.

**"Line N of … is not a valid Fernet key"** — that line is malformed. A key is
44 characters of url-safe base64; a copy-paste that dropped the trailing `=`
is the usual cause.

**A restored database backup will not decrypt** — it was encrypted under an
older key. Add that key to the ring as a second line, start, run step 4, then
remove it. Keep retired keys as long as you keep the backups.

**Interrupted step 4** — nothing was committed unless the transaction
completed, and the script is resumable. Run the dry run again to see what is
left.

## What this does not protect against

The same boundary as the rest of `crypto.py`: an attacker who can read *both*
the ciphertext and `master.key` has both halves. Rotation limits the value of a
key that leaked on its own — a stolen backup, a mis-scoped file permission, an
old disk image. It is not a defence against a live host compromise.
