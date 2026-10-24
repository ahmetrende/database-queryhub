"""The end of an auto-approve rule against a real database.

The application refuses to make a rule with no end, or a rule for tier DDL
(`auto_approve_limits`). The database does not: a DBA can insert a row with SQL
and leave `expires_at` empty, on purpose, and every reader then treats it as a
permanent rule (docs/OPERATIONS.md §30). Here the real schema proves it:

  * a direct INSERT with `expires_at` NULL succeeds. So does one for tier ddl.
  * the readers (`auto_approve.active_grants`, `effective_grant` and the
    `v_active_auto_approve` view) see such a row as live and permanent.
  * no CHECK constraint says the end is required, and the columns are nullable.
  * the application's insert helper refuses the same row.
  * migration 193 seeded `auto_approve_max_days`.

Every row uses ids that no real person or target has. The clean-up deletes only
those rows, and the principal rows that the migration-109 mirror made for them.
"""
import os

import pytest
from fastapi import HTTPException

from queryhub import auto_approve, auto_approve_limits, db
from queryhub import config as cfg
from queryhub.web import routes_admin as ra

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

TARGET = "it-auto-end-target"
USER = "U0ITAEND01"
OTHER = "U0ITAEND02"
ADMIN = "U0ITAEND99"
IDS = [USER, OTHER, ADMIN]

INSERT = ("INSERT INTO auto_approve_grants (slack_user_id, max_tier, target_server_id, "
          "  database_name, expires_at, reason, granted_by) "
          "VALUES (%s, %s, %s, NULL, {end}, 'integration test', %s) RETURNING id")


def _wipe():
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        cur.execute("SELECT id FROM target_servers WHERE alias = %s", (TARGET,))
        tids = [r["id"] for r in cur.fetchall()]
        cur.execute("DELETE FROM auto_approve_grants WHERE slack_user_id = ANY(%s)", (IDS,))
        cur.execute("SELECT principal_id FROM principal_identity "
                    " WHERE provider = 'slack' AND external_id = ANY(%s)", (IDS,))
        pids = [r["principal_id"] for r in cur.fetchall()]
        cur.execute("DELETE FROM access_grant WHERE principal_id = ANY(%s) OR target_id = ANY(%s)",
                    (pids, tids))
        for sql in ("DELETE FROM principal_setting WHERE principal_id = ANY(%s)",
                    "DELETE FROM principal_identity WHERE principal_id = ANY(%s)",
                    "DELETE FROM principal WHERE id = ANY(%s)"):
            cur.execute(sql, (pids,))
        cur.execute("DELETE FROM auth_event_outbox WHERE slack_user_id = ANY(%s)", (IDS,))
        cur.execute("DELETE FROM target_servers WHERE alias = %s", (TARGET,))


@pytest.fixture
def world():
    _wipe()
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        cur.execute("INSERT INTO target_servers (alias, host, default_database, username, "
                    "  password_encrypted, enabled) "
                    "VALUES (%s, 'it.example.internal', 'd', 'unused', 'unused', FALSE) "
                    "RETURNING id", (TARGET,))
        target = cur.fetchone()["id"]
    cfg.invalidate_cache()
    yield {"target": target}
    _wipe()


def _insert(target, *, user=USER, tier="ro", end="NULL"):
    """The statement a DBA writes by hand. `end` is SQL text."""
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        cur.execute(INSERT.format(end=end), (user, tier, target, ADMIN))
        return cur.fetchone()["id"]


def test_a_direct_insert_with_no_end_succeeds(world):
    new_id = _insert(world["target"])
    row = db.fetch_one("SELECT expires_at, enabled FROM auto_approve_grants WHERE id = %s", (new_id,))
    assert row["expires_at"] is None and row["enabled"] is True


def test_the_readers_see_such_a_row_as_a_permanent_rule(world):
    new_id = _insert(world["target"])
    live = [r for r in auto_approve.active_grants(USER) if r["id"] == new_id]
    assert len(live) == 1 and live[0]["expires_at"] is None
    got = auto_approve.effective_grant(USER, "ro", target_server_id=world["target"])
    assert got is not None and got["id"] == new_id and got["expires_at"] is None
    assert auto_approve.effective_grant(USER, "rw", target_server_id=world["target"]) is None
    view = db.fetch_one("SELECT expires_at FROM v_active_auto_approve WHERE grant_id = %s", (new_id,))
    assert view is not None and view["expires_at"] is None


def test_a_row_for_ddl_made_with_sql_still_works_for_the_readers(world):
    """The app makes no DDL rule (tier_not_allowed). The readers are unchanged,
    so a DBA's own row for DDL still decides."""
    new_id = _insert(world["target"], tier="ddl", end="NOW() + INTERVAL '1 day'")
    got = auto_approve.effective_grant(USER, "ddl", target_server_id=world["target"])
    assert got is not None and got["id"] == new_id and got["max_tier"] == "ddl"


def test_the_database_has_no_rule_about_the_end(world):
    cols = {(r["table_name"], r["column_name"]): r["is_nullable"] for r in db.fetch_all(
        "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
        " WHERE (table_name, column_name) IN (('auto_approve_grants', 'expires_at'), "
        "                                     ('access_grant', 'valid_until'))")}
    assert cols == {("auto_approve_grants", "expires_at"): "YES",
                    ("access_grant", "valid_until"): "YES"}
    checks = [r["def"] for r in db.fetch_all(
        "SELECT pg_get_constraintdef(c.oid) AS def FROM pg_constraint c "
        "  JOIN pg_class t ON t.oid = c.conrelid "
        " WHERE c.contype = 'c' AND t.relname IN ('auto_approve_grants', 'access_grant')")]
    assert not [d for d in checks if "expires_at IS NOT NULL" in d or "valid_until IS NOT NULL" in d], checks
    # And no trigger on the table names the rule.
    trig = [r["def"] for r in db.fetch_all(
        "SELECT pg_get_triggerdef(g.oid) AS def FROM pg_trigger g "
        "  JOIN pg_class t ON t.oid = g.tgrelid "
        " WHERE NOT g.tgisinternal AND t.relname = 'auto_approve_grants'")]
    assert not [d for d in trig if "end_required" in d or "max_days" in d], trig


def test_the_application_refuses_the_row_that_sql_allows(world):
    before = db.fetch_one("SELECT count(*) AS n FROM auto_approve_grants WHERE slack_user_id = %s",
                          (OTHER,))["n"]
    with pytest.raises(HTTPException) as e:
        with db.transaction() as cur:
            ra._insert_user_waiver(cur, OTHER, "ro", world["target"], None, None, None,
                                   "integration test", ADMIN)
    assert e.value.status_code == 400 and e.value.detail["code"] == "end_required"
    with pytest.raises(HTTPException) as e:
        with db.transaction() as cur:
            ra._insert_user_waiver(cur, OTHER, "ddl", world["target"], None, None, 60,
                                   "integration test", ADMIN)
    assert e.value.detail["code"] == "tier_not_allowed"
    after = db.fetch_one("SELECT count(*) AS n FROM auto_approve_grants WHERE slack_user_id = %s",
                         (OTHER,))["n"]
    assert after == before


def test_the_application_writes_a_rule_with_an_end(world):
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        new_id = ra._insert_user_waiver(cur, OTHER, "ro", world["target"], None, None, 60,
                                        "integration test", ADMIN)
    row = db.fetch_one("SELECT expires_at > NOW() AS ahead, "
                       "       expires_at < NOW() + INTERVAL '2 hours' AS soon "
                       "  FROM auto_approve_grants WHERE id = %s", (new_id,))
    assert row["ahead"] and row["soon"]


def test_migration_193_seeded_the_limit(world):
    row = db.fetch_one("SELECT value, description FROM bot_config WHERE key = 'auto_approve_max_days'")
    assert row is not None and row["description"]
    cfg.invalidate_cache()
    assert auto_approve_limits.max_days() == int(row["value"])
