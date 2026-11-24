"""The approval cache has three layers for a repeat RO query.

exact      only literal values differ (ast_safety.fingerprint, mig 044)
canonical  only the form differs (ast_safety.canonical_fingerprint, mig 223)
footprint  earlier completed requests read every column of this one
           (ast_safety.footprint + footprint_covered). It starts in shadow
           mode: the would-be match is stored, and the request still waits.
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from queryhub import ast_safety, auto_approve, core_submit as cs, db, targets
from queryhub import config as cfg

K1 = ast_safety.canonical_fingerprint
FP = ast_safety.footprint
NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Layer 1: the canonical form
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("SELECT a, b FROM t WHERE id = 1 ORDER BY a",
     "SELECT a, b FROM t WHERE id = 2 ORDER BY b DESC"),
    ("SELECT a AS x, b FROM t", "SELECT b, a AS y FROM t"),
    ("SELECT a FROM t WHERE id IN (1, 2, 3)", "SELECT a FROM t WHERE id IN (4)"),
    ("SELECT a FROM t WHERE x = 1 AND y = 2", "SELECT a FROM t WHERE y = 3 AND x = 4"),
    ("SELECT a FROM t WHERE (x = 1 OR y = 2) AND z = 3",
     "SELECT a FROM t WHERE z = 1 AND (y = 1 OR x = 2)"),
    ("SELECT u.a FROM users u WHERE u.id = 1", "SELECT x.a FROM users x WHERE x.id = 2"),
    ("SELECT a.x, b.y FROM a JOIN b ON a.id = b.aid JOIN c ON c.id = b.cid",
     "SELECT a.x, b.y FROM c JOIN b ON c.id = b.cid INNER JOIN a ON a.id = b.aid"),
    ("SELECT A FROM T -- note", "select a from t"),
], ids=["order-by", "select-list", "in-list", "and", "and-or", "table-alias",
        "inner-joins", "comment-and-case"])
def test_these_differ_only_in_form(a, b):
    assert K1(a) is not None and K1(a) == K1(b)


@pytest.mark.parametrize("a,b", [
    ("SELECT a FROM t", "SELECT b FROM t"),
    ("SELECT a FROM t LIMIT 5", "SELECT a FROM t"),
    ("SELECT a.x FROM a LEFT JOIN b ON a.id = b.aid",
     "SELECT a.x FROM b LEFT JOIN a ON a.id = b.aid"),
    ("SELECT * FROM (SELECT a FROM t ORDER BY a LIMIT 5) s",
     "SELECT * FROM (SELECT a FROM t ORDER BY b LIMIT 5) s"),
    ("SELECT a, count(*) FROM t GROUP BY 1", "SELECT count(*), a FROM t GROUP BY 1"),
    ('SELECT a FROM "T"', "SELECT a FROM t"),
    ("SELECT a FROM t WHERE x = 1", "SELECT a FROM t WHERE x = 1 OR y = 2"),
    ("SELECT x.a FROM t1 x JOIN t1 y ON x.id = y.pid",
     "SELECT y.a FROM t1 x JOIN t1 y ON x.id = y.pid"),
], ids=["column", "limit-presence", "outer-join-order", "inner-order-by",
        "positional-group-by", "quoted-name", "added-or", "self-join-aliases"])
def test_these_read_different_data(a, b):
    assert K1(a) != K1(b)


def test_more_than_one_statement_has_no_canonical_form():
    assert K1("SELECT 1; SELECT 2") is None
    assert K1("") is None
    assert K1("SELEC broken FROM") is None


def test_the_dialect_is_part_of_the_hash():
    assert K1("SELECT a FROM t", "postgres") != K1("SELECT a FROM t", "mssql")


# ---------------------------------------------------------------------------
# Layer 2: the footprint
# ---------------------------------------------------------------------------

def test_footprint_names_each_column_and_its_use():
    fp = FP("SELECT u.id, u.name, count(o.id) FROM users u JOIN orders o ON o.uid = u.id "
            "WHERE u.created_at > now() GROUP BY u.id, u.name ORDER BY 3 DESC")
    assert fp["complete"] is True
    assert fp["tables"] == ["orders", "users"]
    cols = {tuple(c) for c in fp["cols"]}
    assert {("users", "id", "raw"), ("users", "name", "raw"), ("orders", "id", "agg"),
            ("orders", "uid", "filter"), ("users", "created_at", "filter")} <= cols


def test_star_reads_every_column():
    assert FP("SELECT * FROM users WHERE id = 3")["cols"] == [
        ["users", "*", "raw"], ["users", "id", "filter"]]


def test_an_aggregate_in_a_window_is_raw():
    cols = {tuple(c) for c in FP("SELECT name, sum(x) OVER (PARTITION BY y) AS s FROM t "
                                 "ORDER BY s")["cols"]}
    assert ("t", "x", "raw") in cols
    assert not any(c[1] == "s" for c in cols), "an ORDER BY alias is not a column"


@pytest.mark.parametrize("sql", [
    "SELECT a FROM t WHERE id IN (SELECT id FROM u)",
    "WITH x AS (SELECT a FROM t) SELECT a FROM x",
    "SELECT a FROM t UNION SELECT a FROM u",
    "SELECT name FROM a JOIN b ON a.id = b.aid",          # whose name?
    "SELECT * FROM generate_series(1, 3)",
])
def test_these_footprints_are_incomplete(sql):
    fp = FP(sql)
    assert fp is None or fp["complete"] is False


POOL = [(10, FP("SELECT id, name, email FROM users")),
        (11, FP("SELECT count(*) FROM orders WHERE uid = 1"))]


def test_a_subset_of_earlier_columns_is_covered():
    assert ast_safety.footprint_covered(
        FP("SELECT name FROM users WHERE email = 'x' ORDER BY id"), POOL) == [10]


def test_coverage_can_come_from_several_requests():
    got = ast_safety.footprint_covered(
        FP("SELECT u.name FROM users u JOIN orders o ON o.uid = u.id"), POOL)
    assert sorted(got) == [10, 11]


@pytest.mark.parametrize("sql", [
    "SELECT phone FROM users",                      # a new column
    "SELECT uid FROM orders",                       # filter before, raw now
    "SELECT name FROM customers",                   # a new table
    "SELECT * FROM users",                          # star needs a star
    "SELECT a FROM t WHERE id IN (SELECT id FROM u)",
])
def test_these_are_not_covered(sql):
    assert ast_safety.footprint_covered(FP(sql), POOL) is None


def test_a_star_covers_raw_columns_of_its_table():
    pool = [(5, FP("SELECT * FROM users"))]
    assert ast_safety.footprint_covered(FP("SELECT phone FROM users"), pool) == [5]
    assert ast_safety.footprint_covered(FP("SELECT phone FROM orders"), pool) is None


def test_agg_does_not_cover_raw():
    pool = [(5, FP("SELECT avg(salary) FROM staff"))]
    assert ast_safety.footprint_covered(FP("SELECT max(salary) FROM staff"), pool) == [5]
    assert ast_safety.footprint_covered(FP("SELECT salary FROM staff"), pool) is None


# ---------------------------------------------------------------------------
# fingerprint_cache_hit: the order of the layers
# ---------------------------------------------------------------------------

@pytest.fixture
def lookups(monkeypatch):
    st = {"exact": None, "canonical": None, "pool": [], "sql": [],
          "settings": {}}

    def fetch_one(sql, params=None):
        st["sql"].append(sql)
        return st["canonical"] if "query_canonical_fp" in sql else st["exact"]

    def fetch_all(sql, params=None):
        st["sql"].append(sql)
        st["pool_params"] = params
        return st["pool"]
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: st["settings"].get(k, d))
    monkeypatch.setattr(cfg, "get_int", lambda k, d=None: d)
    return st


def _hit(**kw):
    args = {"canonical": "c1", "footprint": FP("SELECT name FROM users"),
            "default_schema": None, "risk_summary": None}
    args.update(kw)
    return auto_approve.fingerprint_cache_hit("U1", 7, "db", "f0", **args)


def test_an_exact_match_wins_first(lookups):
    lookups["exact"] = {"id": 1, "completed_at": NOW}
    lookups["canonical"] = {"id": 2, "completed_at": NOW}
    assert _hit() == {"id": 1, "completed_at": NOW, "layer": "exact"}
    assert len(lookups["sql"]) == 1


def test_then_the_canonical_form(lookups):
    lookups["canonical"] = {"id": 2, "completed_at": NOW}
    assert _hit()["layer"] == "canonical"


def test_the_canonical_layer_has_its_own_switch(lookups):
    lookups["canonical"] = {"id": 2, "completed_at": NOW}
    lookups["settings"]["approval_cache_canonical"] = "off"
    lookups["settings"]["approval_cache_footprint"] = "off"
    assert _hit() is None


def _pool_row(rid, sql, days_ago=1):
    return {"id": rid, "completed_at": NOW - timedelta(days=days_ago),
            "query_footprint": FP(sql)}


def test_the_footprint_layer_is_shadow_by_default(lookups):
    lookups["pool"] = [_pool_row(30, "SELECT id, name FROM users")]
    got = _hit()
    assert got["layer"] == "footprint" and got["shadow"] is True
    assert got["matched"] == [30]
    assert lookups["pool_params"][3] == "", "the schema is part of the scope"


def test_the_footprint_layer_on(lookups):
    lookups["settings"]["approval_cache_footprint"] = "on"
    lookups["pool"] = [_pool_row(30, "SELECT id, name FROM users")]
    got = _hit(default_schema="sales")
    assert got["shadow"] is False and got["id"] == 30
    assert lookups["pool_params"][3] == "sales"


@pytest.mark.parametrize("mode", ["off", "maybe", ""])
def test_any_other_mode_is_off(lookups, mode):
    lookups["settings"]["approval_cache_footprint"] = mode
    lookups["pool"] = [_pool_row(30, "SELECT id, name FROM users")]
    assert _hit() is None


def test_a_risk_warning_stops_the_footprint_layer(lookups):
    lookups["pool"] = [_pool_row(30, "SELECT id, name FROM users")]
    assert _hit(risk_summary=":warning: Seq scan on `users`") is None


def test_the_master_switch_stops_every_layer(lookups):
    lookups["settings"]["fingerprint_cache_enabled"] = "off"
    lookups["exact"] = {"id": 1, "completed_at": NOW}
    assert _hit() is None and lookups["sql"] == []


def test_labels_name_the_layer():
    assert "fingerprint match" in auto_approve.decided_by_name_for_fingerprint(5)
    assert "another form" in auto_approve.decided_by_name_for_fingerprint(5, "canonical")
    label = auto_approve.decided_by_name_for_fingerprint(5, "footprint", [5, 6, 7, 8, 9, 10])
    assert "#5, #6, #7, #8, #9 and 1 more" in label


# ---------------------------------------------------------------------------
# create_request: what is stored and recorded
# ---------------------------------------------------------------------------

LEDGER = targets.TargetServer(
    id=7, alias="example-postgres", host="postgres.example.test", port=5432,
    default_database="ledger", username="reader", enabled=True, notes=None,
    engine="postgres", engine_config={})


class _Cur:
    def __init__(self, box):
        self.box, self.rowcount, self.last = box, 1, ""

    def execute(self, sql, params=None):
        self.last = " ".join(sql.split())
        self.box["sql"].append((self.last, params))

    def fetchone(self):
        if self.last.startswith("SELECT count(*)"):
            return {"n": 0}
        if self.last.startswith("INSERT INTO requests"):
            return {"id": 42}
        return None


@pytest.fixture
def create(monkeypatch):
    st = {"hit": None, "sql": [], "audit": [], "asked": []}

    @contextlib.contextmanager
    def txn():
        yield _Cur(st)
    monkeypatch.setattr(cs.db, "transaction", txn)
    monkeypatch.setattr(cs.auto_approve, "decision", lambda *a, **k: None)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)

    def hit(*a, **k):
        st["asked"].append(k)
        return st["hit"]
    monkeypatch.setattr(cs.auto_approve, "fingerprint_cache_hit", hit)
    monkeypatch.setattr(cs.audit, "log_in",
                        lambda cur, rid, actor, name, action, details=None:
                        st["audit"].append((action, details)))

    def go(sql="SELECT name FROM users ORDER BY id", schema=None):
        st["sql"].clear()
        st["audit"].clear()
        prep = cs.Prepared(
            user_id="U1", user_name="Ex", target=LEDGER, database="ledger",
            query=sql, required_mode="ro", justification=None, wants_result=True,
            result_format="csv", sched_for=None, explain_plan=None,
            risk_summary=None, origin="web", default_schema=schema)
        out = cs.create_request(prep)
        assert isinstance(out, cs.Outcome), out
        insert = next((s, p) for s, p in st["sql"] if s.startswith("INSERT INTO requests"))
        return out, insert
    st["go"] = go
    return st


def _value(insert, column):
    sql, params = insert
    cols = sql[sql.index("(") + 1:sql.index(")")].replace(" ", "").split(",")
    return params[cols.index(column)]


def test_every_ro_request_stores_both_new_keys(create):
    out, insert = create["go"]()
    assert out.auto_approved is False
    assert _value(insert, "query_canonical_fp") == K1("SELECT name FROM users ORDER BY id")
    assert json.loads(_value(insert, "query_footprint"))["cols"] == [
        ["users", "id", "filter"], ["users", "name", "raw"]]
    assert _value(insert, "approval_shadow") is None
    asked = create["asked"][0]
    assert asked["canonical"] == _value(insert, "query_canonical_fp")


def test_the_schema_scopes_the_canonical_key(create):
    _out, insert = create["go"](schema="sales")
    assert _value(insert, "query_canonical_fp").endswith("|schema=sales")


def test_a_shadow_match_waits_and_is_recorded(create):
    create["hit"] = {"id": 30, "completed_at": NOW, "layer": "footprint",
                     "matched": [30, 31], "shadow": True}
    out, insert = create["go"]()
    assert out.auto_approved is False and out.fp_hit is None
    assert json.loads(_value(insert, "approval_shadow")) == {
        "layer": "footprint", "matched": [30, 31]}
    assert [a for a, _d in create["audit"]] == ["submitted"]


@pytest.mark.parametrize("layer,matched", [("canonical", None), ("footprint", [30, 31])])
def test_a_live_match_names_its_layer(create, layer, matched):
    create["hit"] = {"id": 30, "completed_at": NOW, "layer": layer,
                     "matched": matched, "shadow": False}
    out, insert = create["go"]()
    assert out.auto_approved is True
    details = dict(create["audit"])["auto_approved_fingerprint"]
    assert details["cache_layer"] == layer
    assert details["fingerprint_match_request_id"] == 30
    assert details.get("cache_matched_request_ids") == matched
    assert _value(insert, "decided_by_name") == auto_approve.decided_by_name_for_fingerprint(
        30, layer, matched)


def test_the_requester_reason_names_the_layer():
    assert "another form" in cs._cache_why({"id": 3, "layer": "canonical"})
    assert "#3, #4" in cs._cache_why({"id": 3, "layer": "footprint", "matched": [3, 4]})
    assert cs._cache_why({"id": 3}) == "(matches your completed request #3)"
