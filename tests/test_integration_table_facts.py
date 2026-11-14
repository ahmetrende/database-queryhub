"""table_facts and pg_ddl against a real PostgreSQL, as `schema_tools` runs them (M13).

Run with QH_RUN_INTEGRATION=1 and QH_DDL_PG_DSN pointing at a THROWAWAY
server. The test creates the schema qh_facts and the role qh_facts_blind and
drops them at the end.
"""
import json
import os

import psycopg
import pytest

from queryhub import pg_ddl, table_facts

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (os.environ.get("QH_RUN_INTEGRATION") and os.environ.get("QH_DDL_PG_DSN")),
                       reason="set QH_RUN_INTEGRATION=1 and QH_DDL_PG_DSN (a throwaway server) to run"),
]

# A value that must never come back: it is in the table and in pg_stats.
SECRET = "jordan.ray@example.com"

SETUP = f"""
DROP SCHEMA IF EXISTS qh_facts CASCADE;
DROP ROLE IF EXISTS qh_facts_blind;
CREATE SCHEMA qh_facts;
CREATE TABLE qh_facts."Order Lines" (
  id int PRIMARY KEY,
  email text NOT NULL,
  state text NOT NULL DEFAULT 'new' CHECK (state IN ('new', 'paid')),
  total numeric(12, 2)
);
CREATE INDEX lines_by_state ON qh_facts."Order Lines" (state);
CREATE FUNCTION qh_facts.touch() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END';
CREATE TRIGGER touch_lines BEFORE UPDATE ON qh_facts."Order Lines"
  FOR EACH ROW EXECUTE FUNCTION qh_facts.touch();
CREATE TRIGGER off_lines AFTER INSERT ON qh_facts."Order Lines"
  FOR EACH ROW EXECUTE FUNCTION qh_facts.touch();
ALTER TABLE qh_facts."Order Lines" DISABLE TRIGGER off_lines;
INSERT INTO qh_facts."Order Lines"
  SELECT g, CASE WHEN g % 2 = 0 THEN '{SECRET}' ELSE 'x' || g || '@example.com' END,
         CASE WHEN g % 3 = 0 THEN 'paid' ELSE 'new' END, g * 1.5
    FROM generate_series(1, 500) g;
ANALYZE qh_facts."Order Lines";
SELECT count(*) FROM qh_facts."Order Lines" WHERE state = 'paid';
CREATE VIEW qh_facts.paid AS SELECT id, total FROM qh_facts."Order Lines" WHERE state = 'paid';
CREATE ROLE qh_facts_blind;
GRANT USAGE ON SCHEMA qh_facts TO qh_facts_blind;
"""


@pytest.fixture(scope="module")
def dsn():
    with psycopg.connect(os.environ["QH_DDL_PG_DSN"], autocommit=True) as cn:
        cn.execute(SETUP)
    yield os.environ["QH_DDL_PG_DSN"]
    with psycopg.connect(os.environ["QH_DDL_PG_DSN"], autocommit=True) as cn:
        cn.execute("DROP SCHEMA IF EXISTS qh_facts CASCADE; DROP ROLE IF EXISTS qh_facts_blind")


def _read(dsn, schema, table, role=None):
    """What schema_tools._live_postgres reads: one READ ONLY transaction."""
    with psycopg.connect(dsn) as cn, cn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        if role:
            cur.execute(f"SET ROLE {role}")
        got = pg_ddl.object_ddl(cur, schema, table)
        facts = table_facts.pg_facts(cur, schema, table)
    return got, facts


def test_a_table_gives_triggers_numeric_statistics_activity_and_index_use(dsn):
    got, facts = _read(dsn, "qh_facts", "Order Lines")
    assert got[0] == "table" and "CHECK" in got[1]
    assert [(t["name"], t["enabled"]) for t in facts["triggers"]] == [
        ("off_lines", False), ("touch_lines", True)]
    assert facts["triggers"][1]["def"].startswith("CREATE TRIGGER touch_lines BEFORE UPDATE")
    stats = {s["column"]: s for s in facts["columnStats"]}
    assert set(stats) == {"id", "email", "state", "total"}
    assert stats["id"]["distinct"] == -1.0 and stats["id"]["nullFraction"] == 0.0
    assert stats["state"]["distinct"] == 2.0
    assert isinstance(stats["email"]["avgWidth"], int)
    assert -1.0 <= stats["id"]["correlation"] <= 1.0
    assert facts["activity"]["liveRows"] == 500
    assert facts["activity"]["lastAnalyze"] is not None
    assert {i["name"] for i in facts["indexUse"]} == {"Order Lines_pkey", "lines_by_state"}
    assert all(isinstance(i["bytes"], int) for i in facts["indexUse"])


def test_no_value_of_the_table_comes_back(dsn):
    """`email` holds SECRET in half of the rows, so pg_stats lists it in
    most_common_vals. The facts must not carry it."""
    with psycopg.connect(dsn) as cn:
        mcv = cn.execute("SELECT most_common_vals::text FROM pg_stats "
                         "WHERE schemaname = 'qh_facts' AND attname = 'email'").fetchone()[0]
    assert SECRET in mcv, "the fixture must put the value in pg_stats"
    _got, facts = _read(dsn, "qh_facts", "Order Lines")
    text = json.dumps(facts, default=str)
    assert SECRET not in text and "@example.com" not in text


def test_a_login_without_select_gets_no_statistics_and_no_error(dsn):
    """pg_stats shows a column only to a login that may read it. The rest of
    the facts still come back."""
    _got, facts = _read(dsn, "qh_facts", "Order Lines", role="qh_facts_blind")
    assert facts["columnStats"] == []
    assert len(facts["triggers"]) == 2


def test_a_view_has_its_script_and_no_activity(dsn):
    got, facts = _read(dsn, "qh_facts", "paid")
    assert got[0] == "view"
    assert facts["triggers"] == [] and facts["indexUse"] == []
    assert facts["activity"] is None or facts["activity"]["liveRows"] in (0, None)


def test_a_missing_relation_is_none(dsn):
    got, facts = _read(dsn, "qh_facts", "gone")
    assert got is None and facts is None
