#!/usr/bin/env python3
"""Make a fresh QueryHub installation immediately usable — DEMO ONLY.

Registers the demo target (all three credential tiers), whitelists a demo
developer, makes a demo admin, and grants the developer RO on the target so the
first query runs without a round trip. Everything it creates is named `demo` and
carries a note saying so.

This exists because "clone and see a screen" was impossible before: the install
needed a pre-provisioned metadata database, a second target database, per-tier
roles, an encrypted credential row and an admin — perhaps 40 minutes of reading
before the first SELECT. docker-compose.yml runs this at first boot.

Refuses to run unless QH_DEMO=1 is set, so it cannot be mistaken for a
provisioning tool. Idempotent: safe to run on every container start.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dba_slack_bot import db  # noqa: E402
from dba_slack_bot.crypto import encrypt  # noqa: E402

DEMO_ALIAS = os.environ.get("QH_DEMO_TARGET_ALIAS", "demo-postgres")
DEMO_HOST = os.environ.get("QH_DEMO_TARGET_HOST", "target")
DEMO_PORT = int(os.environ.get("QH_DEMO_TARGET_PORT", "5432"))
DEMO_DB = os.environ.get("QH_DEMO_TARGET_DB", "shop")
DEMO_ADMIN = os.environ.get("QH_DEMO_ADMIN_USER", "demo-admin")
DEMO_DEV = os.environ.get("QH_DEMO_DEV_USER", "demo-dev")

NOTE = "DEMO ONLY — created by scripts/demo_bootstrap.py. Do not use in production."


def _principal(username: str) -> str:
    return f"local:{username}"


def main() -> int:
    if os.environ.get("QH_DEMO") != "1":
        print("refusing to run: set QH_DEMO=1 to confirm this is a demo install",
              file=sys.stderr)
        return 2

    ro_pw = os.environ.get("QH_DEMO_RO_PASSWORD", "demo-ro")
    rw_pw = os.environ.get("QH_DEMO_RW_PASSWORD", "demo-rw")
    ddl_pw = os.environ.get("QH_DEMO_DDL_PASSWORD", "demo-ddl")

    with db.transaction() as cur:
        # ---- the target, with a credential per tier ----------------------
        cur.execute(
            "INSERT INTO target_servers "
            "  (alias, host, port, default_database, username, "
            "   password_encrypted, notes, enabled, engine) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, 'postgres') "
            "ON CONFLICT (alias) DO UPDATE "
            "  SET host = EXCLUDED.host, port = EXCLUDED.port, "
            "      default_database = EXCLUDED.default_database, "
            "      username = EXCLUDED.username, "
            "      password_encrypted = EXCLUDED.password_encrypted, "
            "      enabled = TRUE "
            "RETURNING id",
            (DEMO_ALIAS, DEMO_HOST, DEMO_PORT, DEMO_DB, "queryhub_ro",
             encrypt(ro_pw), NOTE))
        target_id = cur.fetchone()["id"]

        # RW/DDL live in the per-tier columns when the schema has them; older
        # schemas carry only the base credential, in which case RO is what a
        # demo needs anyway.
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'target_servers' "
            "  AND column_name IN ('username_rw','password_rw_encrypted',"
            "'username_ddl','password_ddl_encrypted')")
        tier_cols = {r["column_name"] for r in cur.fetchall()}
        if {"username_rw", "password_rw_encrypted"} <= tier_cols:
            cur.execute(
                "UPDATE target_servers SET username_rw = %s, "
                "  password_rw_encrypted = %s WHERE id = %s",
                ("queryhub_rw", encrypt(rw_pw), target_id))
        if {"username_ddl", "password_ddl_encrypted"} <= tier_cols:
            cur.execute(
                "UPDATE target_servers SET username_ddl = %s, "
                "  password_ddl_encrypted = %s WHERE id = %s",
                ("queryhub_ddl", encrypt(ddl_pw), target_id))

        # ---- people ------------------------------------------------------
        for username, name in ((DEMO_ADMIN, "Demo Admin"),
                               (DEMO_DEV, "Demo Developer")):
            cur.execute(
                "INSERT INTO requesters (slack_user_id, name, email, enabled, "
                "  added_at, added_by) "
                "VALUES (%s, %s, %s, TRUE, NOW(), 'demo-bootstrap') "
                "ON CONFLICT (slack_user_id) DO UPDATE SET enabled = TRUE",
                (_principal(username), name, f"{username}@example.com"))

        cur.execute(
            "INSERT INTO admins (slack_user_id, name, enabled, added_at, "
            "  added_by, can_grant) "
            "VALUES (%s, %s, TRUE, NOW(), 'demo-bootstrap', TRUE) "
            "ON CONFLICT (slack_user_id) DO UPDATE "
            "  SET enabled = TRUE, can_grant = TRUE",
            (_principal(DEMO_ADMIN), "Demo Admin"))

        # ---- one grant, so the first query is possible --------------------
        # RO only. The demo should show the approval path, not bypass it: an
        # RW/DDL query from the demo developer goes to the admin queue, which is
        # the thing worth looking at.
        cur.execute(
            "INSERT INTO user_target_grants "
            "  (slack_user_id, target_server_id, allowed_databases, mode, "
            "   granted_at, granted_by, revoked_at) "
            "VALUES (%s, %s, NULL, 'ro', NOW(), 'demo-bootstrap', NULL) "
            "ON CONFLICT (slack_user_id, target_server_id) DO UPDATE "
            "  SET mode = 'ro', revoked_at = NULL",
            (_principal(DEMO_DEV), target_id))

        # ---- make the demo actually able to run a query ------------------
        # target_ssl_mode defaults to `require`, which is right for production
        # and fatal here: the demo target is a plain postgres container with no
        # TLS, so every execution failed with "server does not support SSL, but
        # SSL was required" — the demo would have shipped unable to do the one
        # thing it exists to show. `prefer` encrypts when the server offers it
        # and proceeds when it does not.
        cur.execute(
            "INSERT INTO bot_config (key, value, description) VALUES "
            "  ('target_ssl_mode', 'prefer', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = 'prefer'",
            ("DEMO: relaxed from 'require' because the demo target container "
             "has no TLS. Production installs should use require or "
             "verify-full.",))

        cur.execute(
            "INSERT INTO audit_log (actor_slack_id, actor_name, action, details) "
            "VALUES ('demo-bootstrap', 'demo bootstrap', 'demo_seeded', "
            "        %s::jsonb)",
            ('{"target": "%s", "admin": "%s", "developer": "%s"}'
             % (DEMO_ALIAS, DEMO_ADMIN, DEMO_DEV),))

    print(f"demo target '{DEMO_ALIAS}' registered (id={target_id}) -> "
          f"{DEMO_HOST}:{DEMO_PORT}/{DEMO_DB}")
    print(f"demo admin     : {_principal(DEMO_ADMIN)}")
    print(f"demo developer : {_principal(DEMO_DEV)} (RO on {DEMO_ALIAS})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
