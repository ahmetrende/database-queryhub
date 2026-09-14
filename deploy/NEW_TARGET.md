# QueryHub — onboarding a new target cluster

Run this when a new Postgres cluster should become queryable through
`/sql`. Two sides: **cluster-side** (create the bot's login roles) and
**bot-DB side** (register the cluster + grant access). Neither is
automatic — the bot doesn't discover clusters.

> All hosts, passwords, aliases, team names, and IPs below are
> placeholders. Substitute real values at run time; never commit them.

## 0. Prerequisites
- [ ] Admin connection to the new cluster (`rds_superuser` on RDS)
- [ ] The bot host's private IP is in the new cluster's security group
      / firewall (port 5432). Without this the bot can't reach it.
- [ ] Decide which databases on the cluster should be bot-readable
      (skip `template0`, `template1`, `rdsadmin`, and anything hidden).

## 1. Cluster-side — create the three login roles

Pick a strong password per tier; you'll encrypt them in step 2. Run
each script once per target database (the RO script's header has a
bash loop for many DBs at once).

- [ ] **RO** — required:
  ```
  read -s -p 'ro password: ' RO_PASS; echo
  PGPASSWORD=<admin> psql -h <host> -U <admin> -d <db> \
      -v ro_password="$RO_PASS" -f deploy/grant_readonly.sql
  ```
- [ ] **RW** — if the team needs writes:
  ```
  read -s -p 'rw password: ' RW_PASS; echo
  PGPASSWORD=<admin> psql -h <host> -U <admin> -d <db> \
      -v rw_password="$RW_PASS" -f deploy/grant_readwrite.sql
  ```
- [ ] **DDL** — only if the team needs schema changes. The script
      creates the role but you must choose a privilege model in its
      section 5 first (see `deploy/grant_ddl.sql`):
  ```
  read -s -p 'ddl password: ' DDL_PASS; echo
  PGPASSWORD=<admin> psql -h <host> -U <admin> -d <db> \
      -v ddl_password="$DDL_PASS" -f deploy/grant_ddl.sql
  ```

> A tier with no role provisioned simply stays unavailable — the bot
> fails fast with a friendly "not configured" message if someone's
> grant points at a missing credential. So provision only the tiers you
> intend to hand out.

## 2. Bot-DB side — encrypt the passwords

For each tier you created, turn the plaintext password into Fernet
ciphertext (uses the bot's master.key):
```
source .venv/bin/activate && set -a && source /etc/queryhub/env && set +a
python3 scripts/encrypt_secret.py     # paste/enter the password, copy the ciphertext
```
- [ ] RO ciphertext
- [ ] RW ciphertext (if provisioned)
- [ ] DDL ciphertext (if provisioned)

## 3. Bot-DB side — register the cluster

Use the templates in `deploy/db_admin_templates.sql`:
- [ ] `INSERT INTO target_servers` — alias, host, port, default_database,
      `username` = the RO role, `password_encrypted` = RO ciphertext
- [ ] If RW provisioned: `UPDATE target_servers SET username_rw=...,
      password_rw_encrypted=...` with the RW ciphertext
- [ ] If DDL provisioned: `UPDATE target_servers SET username_ddl=...,
      password_ddl_encrypted=...` with the DDL ciphertext
- [ ] Confirm with the inspection SELECT at the bottom of
      `db_admin_templates.sql` (shows which tiers are set)

## 4. Bot-DB side — grant access

Decide who reaches this cluster and at what tier (`deploy/team_admin_templates.sql`):
- [ ] Team-level: `INSERT INTO team_target_grants` (team, target, mode,
      allowed_databases) — most common
- [ ] Or per-user override: `INSERT INTO user_target_grants` for a
      single person (beats the team grant)
- [ ] (Optional) per-team Postgres role fence via
      `deploy/grant_team_role.sql` + `target_role` on the grant

## 5. Smoke test
- [ ] No restart needed — `target_servers` and grants are read live.
- [ ] In Slack: `/sql`, pick the new alias, run a trivial `SELECT 1`,
      approve it, confirm the result DM arrives.
- [ ] If RW/DDL provisioned, test one of each with a harmless statement.

## Notes
- **No restart** for any of this — the bot reads targets + grants per
  request.
- **Read replicas**: register them like any other target, but only
  provision RO (writes would fail at the replica anyway).
- **Password rotation later**: re-run the same grant script with a new
  password (it resets the role's password), re-encrypt, and UPDATE the
  ciphertext on the target row.
- **Pre-commit**: never paste real hostnames / passwords into tracked
  files. `scripts/check_repo_clean.py` must print `clean` before any
  commit.
