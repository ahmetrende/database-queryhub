"""POST /classify — the server-side tier verdict the editor renders.

The browser used to decide RO/RW/DDL itself with a shorter keyword list and a
naive `;` split. That drove the Run-vs-Submit button, so it could show a green
"Run" for a statement the server classifies DDL, and it could *block* a legal
read whose string literal contained a semicolon. This endpoint is the single
source of truth the UI now asks; these tests pin the two payloads that broke.

Route functions are called directly with fake claims — no TestClient, no DB.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dba_slack_bot.web import routes_queries as rq


class _Target:
    def __init__(self, engine="postgres"):
        self.id = 7
        self.alias = "prod-main"
        self.engine = engine
        self.enabled = True
        self.default_database = "payments"


@pytest.fixture
def wire(monkeypatch):
    """Stub everything around query_safety.analyze — that stays real, since it
    is the thing under test."""
    state = {"granted": "ro", "auto": None, "super": False}

    monkeypatch.setattr(rq.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rq, "_target_by_alias",
                        lambda alias: _Target() if alias == "prod-main" else None)
    monkeypatch.setattr(rq.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(rq.admins, "is_super_admin", lambda uid: state["super"])

    import dba_slack_bot.teams as teams_mod
    monkeypatch.setattr(teams_mod, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": state["granted"]})
    monkeypatch.setattr(teams_mod, "effective_mode_for_database",
                        lambda uid, tid, db: state["granted"])

    import dba_slack_bot.auto_approve as aa
    monkeypatch.setattr(aa, "effective_grant",
                        lambda *a, **k: state["auto"])
    return state


def _call(sql, db="payments", conn="prod-main"):
    body = rq.ClassifyIn(connectionId=conn, databaseId=db, sql=sql)
    return rq.classify_query(body, claims={"sub": "U0EXAMPLE01"})


def test_refresh_matview_is_ddl_not_read_only(wire):
    """The exact statement the client classifier called RO: it takes an
    ACCESS EXCLUSIVE lock and must never be presented as an instant read."""
    r = _call("REFRESH MATERIALIZED VIEW big_mv;")
    assert r["tier"] == "DDL"
    assert r["tierExceedsGrant"] is True          # the fixture grants RO
    assert r["willAutoApprove"] is False
    assert r["requiresJustification"] is True


@pytest.mark.parametrize("sql", [
    "VACUUM FULL users;",
    "REINDEX TABLE orders;",
    "ANALYZE users;",
])
def test_maintenance_statements_are_ddl(wire, sql):
    assert _call(sql)["tier"] == "DDL"


def test_semicolon_inside_a_literal_is_one_read(wire):
    """The false-block direction: the client counted two statements here and
    called it DDL, which disabled submit for a query the server accepts."""
    r = _call("SELECT ';DROP TABLE x';")
    assert r["tier"] == "RO"
    assert r["statements"] == 1
    assert r["tierExceedsGrant"] is False
    assert r["blocked"] is False


def test_read_with_matching_grant_and_auto_window_runs(wire):
    wire["auto"] = {"id": 1, "max_tier": "ro"}
    r = _call("SELECT count(*) FROM orders;")
    assert r["tier"] == "RO"
    assert r["willAutoApprove"] is True
    assert r["requiresJustification"] is False


def test_auto_approve_never_claimed_for_a_tier_over_the_grant(wire):
    """A stale auto-approve row must not turn into "Run" for a tier the user
    isn't granted — the gate is grant first, window second."""
    wire["auto"] = {"id": 1, "max_tier": "ddl"}
    wire["granted"] = "ro"
    r = _call("ALTER TABLE events ADD COLUMN device_id text;")
    assert r["tier"] == "DDL"
    assert r["tierExceedsGrant"] is True
    assert r["willAutoApprove"] is False


def test_blocked_query_reports_blockers_and_no_auto_run(wire):
    wire["granted"] = "ddl"
    wire["auto"] = {"id": 1, "max_tier": "ddl"}
    r = _call("SELECT pg_read_file('/etc/passwd');")
    assert r["blocked"] is True
    assert r["blockers"]
    assert r["willAutoApprove"] is False


def test_unknown_connection_is_404_not_a_verdict(wire):
    """Same enumeration guard as /queries and /explain: unknown, disabled and
    ungranted all look identical."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _call("SELECT 1", conn="does-not-exist")
    assert e.value.status_code == 404


def test_database_outside_the_grant_is_404(wire, monkeypatch):
    import dba_slack_bot.teams as teams_mod
    monkeypatch.setattr(teams_mod, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": ["payments"]})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _call("SELECT 1", db="secrets")
    assert e.value.status_code == 404


# ---------------------------------------------------------------------------
# The client-side hint must fail pessimistic. It is not authoritative, but it
# decides what the button says for the ~400ms before the server answers, so
# "unknown keyword" has to mean "needs approval", never "runs instantly".
# The classifier is plain JS inside a .jsx file; lift the pure region and run
# it in node.
# ---------------------------------------------------------------------------
_JS_START = "const QH_DDL_KW"
_JS_END = "// Returns { tier, statements:[{kw,tier}], multi:bool }"


def _classify_js(payloads):
    src = (Path(__file__).resolve().parents[1]
           / "QueryHubWeb" / "qh-data.jsx").read_text(encoding="utf-8")
    start = src.index(_JS_START)
    end = src.index(_JS_END, start)
    region = src[start:end]
    # qhClassify itself sits just after the marker comment; take through the
    # end of its body (the next line that is a lone closing brace).
    rest = src[end:]
    body_end = rest.index("\n}\n") + len("\n}\n")
    region += rest[:body_end]
    script = region + "\nconsole.log(JSON.stringify(" + json.dumps(payloads) \
        + ".map(s => qhClassify(s).tier)));"
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_client_hint_is_pessimistic_about_unknown_keywords():
    tiers = _classify_js([
        "SELECT 1",                              # known read
        "REFRESH MATERIALIZED VIEW mv;",         # was RO before the fix
        "VACUUM FULL users;",
        "REINDEX TABLE orders;",
        "CLUSTER t USING idx;",
        "REASSIGN OWNED BY a TO b;",
        "FLARGLE something;",                    # not a real keyword at all
    ])
    assert tiers[0] == "RO"
    assert tiers[1:] == ["DDL"] * 6


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_client_hint_does_not_split_inside_literals():
    tiers = _classify_js([
        "SELECT ';DROP TABLE x'",
        "SELECT * FROM t WHERE note = 'a;b'",
        "SELECT $$ a; DROP TABLE x $$",
    ])
    assert tiers == ["RO", "RO", "RO"]
