"""A statement sqlparse will not group is refused, not a 500.

sqlparse 0.5 bounds its own running time. It raises SQLParseError for:
- a statement of more than 10,000 tokens (MAX_GROUPING_TOKENS);
- a statement nested more than 100 levels deep (MAX_GROUPING_DEPTH, or
  Python's recursion limit on a long operator chain).

analyze() called it bare. A pasted list of about 3,400 ids therefore escaped
from submit and from the editor's classify call as an unhandled 500. The
person who hit it got the same 500 three times in a row.

The cap stays. Lifted, a list of identifiers of about 120 KB took more than a
minute to parse, and one of 32 KB took 11 s.
"""
import pytest

from queryhub import core_submit as cs
from queryhub import query_safety as qs
from queryhub.web import routes_queries as rq

IDS = [str(100000 + i) for i in range(3400)]


def _in_list(ids, sep=", "):
    return "SELECT * FROM t WHERE id IN (" + sep.join(ids) + ")"


LONG = _in_list(IDS)                       # just over the token cap
DEEP = "SELECT " + "(" * 150 + "1" + ")" * 150
CHAIN = "SELECT 1" + " + 1" * 300


# --- analyze -----------------------------------------------------------------------

@pytest.mark.parametrize("unrestricted", [False, True], ids=["user", "super-admin"])
def test_a_list_over_the_token_cap_is_refused_not_raised(unrestricted):
    r = qs.analyze(LONG, unrestricted=unrestricted)
    assert r.blocked
    assert "too long to check" in r.blockers[0]
    assert not r.statements


def test_the_refusal_offers_the_rewrite_for_the_engine():
    assert "ANY('{1,2,3}'::bigint[])" in qs.analyze(LONG).blockers[0]
    assert "STRING_SPLIT" in qs.analyze(LONG, engine="mssql").blockers[0]
    # No rewrite to offer on this engine: splitting is the only advice.
    msg = qs.analyze(LONG, engine="clickhouse").blockers[0]
    assert "ANY(" not in msg and "STRING_SPLIT" not in msg
    assert msg.endswith("in the same script.")


@pytest.mark.parametrize("sql", [DEEP, CHAIN], ids=["brackets", "operator-chain"])
def test_nesting_past_the_depth_cap_is_refused_not_raised(sql):
    r = qs.analyze(sql)
    assert r.blocked
    assert "nested too deeply" in r.blockers[0]


def test_a_list_under_the_cap_still_reads():
    r = qs.analyze(_in_list(IDS[:3000]))
    assert not r.blocked, r.blockers
    assert r.main_tier == "ro"


@pytest.mark.parametrize("engine, sql", [
    ("postgres",
     "SELECT * FROM t WHERE id = ANY('{" + ",".join(IDS) + "}'::bigint[])"),
    ("postgres",
     ";\n".join(_in_list(IDS[k * 850:(k + 1) * 850]) for k in range(4))),
    ("mssql",
     "SELECT * FROM t WHERE id IN (SELECT value FROM STRING_SPLIT('"
     + ",".join(IDS) + "', ','))"),
], ids=["one-array-value", "split-across-statements", "one-string-value"])
def test_the_rewrites_the_refusal_suggests_pass(engine, sql):
    """Each holds all 3,400 ids. Advice that is refused in turn would leave the
    person where the 500 left them."""
    r = qs.analyze(sql, engine=engine)
    assert not r.blocked, r.blockers
    assert r.main_tier == "ro"


# --- the two routes that returned 500 ----------------------------------------------

class _Target:
    id = 7
    alias = "prod-main"
    engine = "postgres"
    enabled = True
    default_database = "payments"
    host = "h"
    port = 5432


def test_classify_answers_with_the_refusal(monkeypatch):
    monkeypatch.setattr(rq.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rq, "_target_by_alias", lambda alias: _Target())
    monkeypatch.setattr(rq.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(rq.admins, "is_super_admin", lambda uid: False)
    import queryhub.teams as teams_mod
    monkeypatch.setattr(teams_mod, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ro"})
    monkeypatch.setattr(teams_mod, "effective_mode_for_database",
                        lambda uid, tid, db: "ro")

    body = rq.ClassifyIn(connectionId="prod-main", databaseId="payments", sql=LONG)
    r = rq.classify_query(body, claims={"sub": "U0EXAMPLE01"})
    assert r["blocked"] is True
    assert "too long to check" in r["blockers"][0]
    assert r["willAutoApprove"] is False


def test_submit_turns_it_into_a_query_refusal(monkeypatch):
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(cs.requesters, "open_request_count", lambda uid: 0)
    monkeypatch.setattr(cs.cfg, "get_int",
                        lambda k, d=None: {"min_query_length": 1,
                                           "max_open_requests_per_user": 5}
                        .get(k, d if d is not None else 5))
    monkeypatch.setattr(cs.cfg, "get_setting", lambda k, d=None: d)
    monkeypatch.setattr(cs.targets, "get", lambda tid: _Target())
    from queryhub import lifecycle
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)

    out = cs.validate_submission(
        "U0EXAMPLE01", "someone", target_server_id=7, database_name="payments",
        query=LONG, justification=None)
    assert isinstance(out, cs.Rejection), f"got {out!r}"
    assert out.field == "query"
    assert "too long to check" in out.message
