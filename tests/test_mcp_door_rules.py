"""The rules a program meets at the MCP door, before the door opens.

Three groups of rules:

1. A person sees only the databases they may use, also through MCP. One
   function decides the list for the web UI, the MCP tools and the submit
   check (`db_scope`). A database outside the list gets one answer.
2. A write that came through MCP always waits for a person. No waiver, no
   super-admin self-approval and no cache hit replaces the review.
3. `submit_query` classifies on the server and decides. A caller that never
   asked `classify_sql` meets the same checks.

Nothing here may reach a database. A read a test forgot to fake fails it.
"""
from __future__ import annotations

import contextlib
import inspect

import pytest

from queryhub import core_submit as cs
from queryhub import db, db_scope, lifecycle, targets
from queryhub.mcp_server import policy, tools
from queryhub.web import routes_data

ME = "U0EXAMPLE001"
SUPER = "U0EXAMPLE002"
READ = "SELECT count(*) FROM ledgers"


def _target(tid=7, alias="example-pg", engine="postgres", default="ledger", replica_of=None):
    t = targets.TargetServer(
        id=tid, alias=alias, host="pg.example.test", port=5432,
        default_database=default, username="reader", enabled=True, notes=None,
        engine=engine, engine_config={})
    if replica_of is not None:
        object.__setattr__(t, "replica_of", replica_of)
    return t


PG = _target()


def _grant(mode="ro", allowed=None):
    return {"mode": mode, "allowed_databases": allowed, "source": "team"}


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)


# ---------------------------------------------------------------------------
# describe_database
# ---------------------------------------------------------------------------

@pytest.fixture
def describe(monkeypatch):
    st = {"grant": _grant(allowed={"dbA"}), "catalog": ["dbA", "dbB"], "reads": []}
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools, "_granted", lambda uid, c: (PG, st["grant"]))
    monkeypatch.setattr(db_scope, "catalog_databases",
                        lambda tid: st["reads"].append(tid) or list(st["catalog"]))

    def go(database, table=None):
        return tools.describe_database(PG.alias, database, table)
    st["go"] = go
    return st


@pytest.mark.parametrize("database", ["dbB", "postgres", "nope"])
def test_a_database_outside_the_grant_gets_one_answer(describe, database):
    """`dbB` exists, `postgres` is hidden and `nope` does not exist. The
    answer is the same sentence for all three, so the names cannot be told
    apart. It names only the databases the person may use."""
    with pytest.raises(tools.ToolError) as e:
        describe["go"](database)
    assert e.value.code == "no_access"
    assert e.value.message == (f"You have no access to database `{database}` on "
                               "`example-pg`. You can use: `dbA`.")
    assert "dbB" not in e.value.message.replace(f"`{database}`", "")


def test_an_unrestricted_grant_is_checked_against_the_catalog(describe):
    describe["grant"] = _grant(allowed=None)
    describe["catalog"] = ["dbA", "dbB"]
    for database in ("postgres", "nope"):
        with pytest.raises(tools.ToolError) as e:
            describe["go"](database)
        assert e.value.code == "no_access"
    assert describe["reads"] == [PG.id, PG.id], "an unrestricted grant reads the catalog"


def test_a_restricted_grant_needs_no_catalog_read(describe):
    with pytest.raises(tools.ToolError):
        describe["go"]("dbB")
    assert describe["reads"] == []


def test_a_listed_database_with_no_snapshot_says_so(describe, monkeypatch):
    monkeypatch.setattr(tools.db, "fetch_all", lambda *a, **k: [])
    with pytest.raises(tools.ToolError) as e:
        describe["go"]("dbA")
    assert e.value.code == "no_catalog" and "not catalogued yet" in e.value.message


@pytest.mark.parametrize("allowed", [{"dbA"}, None])
def test_a_table_that_matches_nothing_names_the_next_call(describe, monkeypatch, allowed):
    """The refusal names the tool to call next, with a restricted and an
    unrestricted grant."""
    describe["grant"] = _grant(allowed=allowed)
    monkeypatch.setattr(tools.db, "fetch_all", lambda *a, **k: [])
    with pytest.raises(tools.ToolError) as e:
        describe["go"]("dbA", "nothing_like_this")
    assert e.value.code == "no_match"
    assert "call `queryhub_describe_database` without `table`" in e.value.message


# ---------------------------------------------------------------------------
# list_connections
# ---------------------------------------------------------------------------

@pytest.fixture
def listing(monkeypatch):
    st = {"fleet": [PG], "grants": {}, "catalog": {}, "modes": {}}
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools.targets, "list_enabled", lambda: list(st["fleet"]))
    monkeypatch.setattr(tools.teams, "effective_grants_for_user",
                        lambda uid, ids: {i: st["grants"].get(i) for i in ids})
    monkeypatch.setattr(db_scope, "catalog_databases_map", lambda ids: dict(st["catalog"]))
    monkeypatch.setattr(tools.teams, "effective_mode_for_database",
                        lambda uid, tid, d: st["modes"].get((tid, d)))
    monkeypatch.setattr(tools.admins, "is_super_admin", lambda uid: False)
    return st


def test_an_unrestricted_grant_lists_the_catalogued_names(listing):
    listing["grants"][PG.id] = _grant("rw")
    listing["catalog"][PG.id] = ["ledger", "postgres", "audit"]
    (conn,) = tools.list_connections()["connections"]
    assert [d["name"] for d in conn["databases"]] == ["ledger", "postgres"][:1] + ["audit"]
    assert "allDatabases" not in conn
    assert {d["tier"] for d in conn["databases"]} == {"RW"}


def test_a_restricted_grant_lists_its_names_without_the_hidden_one(listing):
    listing["grants"][PG.id] = _grant(allowed={"dbA", "postgres"})
    listing["modes"] = {(PG.id, "dbA"): "ro", (PG.id, "postgres"): "ro"}
    (conn,) = tools.list_connections()["connections"]
    assert [d["name"] for d in conn["databases"]] == ["dbA"]


def test_a_server_with_no_visible_database_does_not_show(listing):
    listing["grants"][PG.id] = _grant(allowed={"postgres"})
    assert tools.list_connections()["connections"] == []


def test_a_server_with_no_grant_does_not_show(listing):
    assert tools.list_connections()["connections"] == []


def test_the_tier_is_shown_for_each_database(listing):
    listing["grants"][PG.id] = _grant("rw", allowed={"dbA", "dbB", "dbC"})
    listing["modes"] = {(PG.id, "dbA"): "rw", (PG.id, "dbB"): "ro"}   # dbC: no tier
    (conn,) = tools.list_connections()["connections"]
    assert [(d["name"], d["tier"]) for d in conn["databases"]] == [("dbA", "RW"), ("dbB", "RO")]


def test_a_read_replica_is_not_a_connection(listing):
    replica = _target(8, "example-pg-replica", replica_of=PG.id)
    listing["fleet"] = [PG, replica]
    listing["grants"] = {PG.id: _grant(allowed={"dbA"}), replica.id: _grant(allowed={"dbA"})}
    listing["modes"] = {(PG.id, "dbA"): "ro", (replica.id, "dbA"): "ro"}
    assert [c["connection"] for c in tools.list_connections()["connections"]] == ["example-pg"]


def test_the_web_and_mcp_lists_come_from_the_same_function():
    """One rule, two callers. The web groups its reads for speed, MCP reads one
    server at a time, and both ask `db_scope.visible_databases`. A refusal
    asks `db_scope.usable_databases`, which asks the same function."""
    assert "db_scope.visible_databases" in inspect.getsource(routes_data.connections)
    assert "db_scope.visible_databases" in inspect.getsource(tools.list_connections)
    assert "db_scope.usable_databases" in inspect.getsource(tools.describe_database)
    assert "visible_databases(" in inspect.getsource(db_scope.usable_databases)
    assert "db_scope.grant_covers" in inspect.getsource(routes_data._granted_db)


# ---------------------------------------------------------------------------
# the submit check
# ---------------------------------------------------------------------------

@pytest.fixture
def submit(monkeypatch):
    st = {"grant": _grant("rw", allowed={"dbA"}), "scope_ok": True,
          "catalog": ["dbA", "dbB"]}
    monkeypatch.setattr(db_scope, "catalog_databases", lambda tid: list(st["catalog"]))
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(cs.requesters, "open_request_count", lambda uid: 0)
    monkeypatch.setattr(cs.targets, "get", lambda tid: PG)
    monkeypatch.setattr(cs.teams, "effective_grant_for_user", lambda uid, tid: st["grant"])
    monkeypatch.setattr(cs.teams, "effective_mode_for_database", lambda *a: "rw")
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: False)
    monkeypatch.setattr(cs.db, "fetch_one", lambda *a, **k: None)

    def validate_scope(tid, name):
        if not st["scope_ok"]:
            raise cs.auto_approve.ScopeError(name)
    monkeypatch.setattr(cs.auto_approve, "validate_scope", validate_scope)

    def go(database):
        return cs.validate_submission(
            ME, "Ex", target_server_id=PG.id, database_name=database, query=READ,
            justification="a month-end check", confirmed=True)
    st["go"] = go
    return st


def test_a_submit_outside_the_grant_names_only_the_usable_databases(submit):
    rej = submit["go"]("dbB")
    assert isinstance(rej, cs.Rejection) and rej.field == "database"
    assert rej.message == ("You have no access to database `dbB` on `example-pg`. "
                           "You can use: `dbA`.")


def test_an_unknown_database_gets_the_same_answer(submit):
    """With a restricted grant, a name that does not exist and a name outside
    the grant get the same sentence."""
    known = submit["go"]("dbB").message.replace("`dbB`", "`X`")
    unknown = submit["go"]("nope").message.replace("`nope`", "`X`")
    assert known == unknown


def test_an_unrestricted_grant_lists_the_catalog(submit):
    """The person may use every catalogued database, so the list tells them
    nothing new about the server."""
    submit["grant"] = _grant("rw", allowed=None)
    submit["scope_ok"] = False
    rej = submit["go"]("nova")
    assert isinstance(rej, cs.Rejection) and rej.field == "database"
    assert rej.message == ("You have no access to database `nova` on `example-pg`. "
                           "You can use: `dbA`, `dbB`.")


def test_a_long_list_is_cut_at_eight():
    usable = [f"db{i:02d}" for i in range(11)]
    msg = db_scope.no_access_message("example-pg", "x", usable)
    assert msg.endswith("`db07` (+3 more).")
    assert "`db08`" not in msg


def test_no_list_gives_the_plain_sentence():
    assert (db_scope.no_access_message("example-pg", "x", [])
            == "You have no access to database `x` on `example-pg`.")


def test_every_refusal_passes_the_usable_list():
    """A refusal without the list falls back to the plain sentence, and the
    person sees no correct name. Every call in `src/` passes the list."""
    import ast
    import pathlib
    src = pathlib.Path(cs.__file__).parent
    calls = []
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "no_access_message"):
                calls.append((path.name, node.lineno, len(node.args) + len(node.keywords)))
    assert len(calls) >= 4, calls
    assert [c for c in calls if c[2] < 3] == []


def test_the_grant_is_checked_before_the_catalog(submit, monkeypatch):
    """A name outside the grant must not reach the catalog lookup, or the
    lookup's own answer would tell the caller whether the database exists."""
    asked = []
    monkeypatch.setattr(cs.auto_approve, "validate_scope",
                        lambda tid, name: asked.append(name))
    assert isinstance(submit["go"]("dbB"), cs.Rejection)
    assert asked == []


def test_a_database_in_the_grant_passes_the_check(submit):
    assert not isinstance(submit["go"]("dbA"), cs.Rejection)


@pytest.mark.parametrize("who", ["super-admin", "waiver"])
def test_an_mcp_write_needs_a_justification_whoever_sends_it(submit, monkeypatch, who):
    """A write from the MCP door always waits for a person, and that person
    reads the reason. Where the web would run the write without review, the
    web still asks for no reason."""
    if who == "super-admin":
        monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: True)
    else:
        monkeypatch.setattr(cs.auto_approve, "decision", lambda *a, **k: {"id": 7})

    def go(origin):
        return cs.validate_submission(
            ME, "Ex", target_server_id=PG.id, database_name="dbA",
            query="UPDATE ledgers SET n = 1 WHERE id = 2", justification=None,
            confirmed=True, origin=origin)
    out = go("mcp")
    assert isinstance(out, cs.Rejection) and out.field == "justification", out
    assert out.message == "Write a justification for RW queries."
    assert isinstance(go("web"), cs.Prepared), "the web keeps the exemption"


# ---------------------------------------------------------------------------
# a write through MCP always waits for a person
# ---------------------------------------------------------------------------

class _Cur:
    def __init__(self, box):
        self.box, self.rowcount, self.last = box, 1, ""

    def execute(self, sql, params=None):
        self.last = " ".join(sql.split())

    def fetchone(self):
        if self.last.startswith("SELECT count(*)"):
            return {"n": 0}
        if self.last.startswith("INSERT INTO requests"):
            return {"id": 42}
        return None


WAIVER = {"id": 501, "slack_user_id": ME, "max_tier": "ddl", "target_server_id": None,
          "database_name": None, "reason": "example", "granted_by": "U0EXAMPLE009"}


@pytest.fixture
def create(monkeypatch):
    st = {"super": set(), "waiver": False, "prior": None, "decided": 0, "audit": []}

    @contextlib.contextmanager
    def txn():
        yield _Cur(st)

    def decision(*a, **k):
        st["decided"] += 1
        return dict(WAIVER) if st["waiver"] else None
    monkeypatch.setattr(cs.db, "transaction", txn)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: uid in st["super"])
    monkeypatch.setattr(cs.auto_approve, "decision", decision)
    monkeypatch.setattr(cs.auto_approve, "fingerprint_cache_hit", lambda *a, **k: st["prior"])
    monkeypatch.setattr(cs.ast_safety, "fingerprint", lambda *a, **k: "fp-1")
    monkeypatch.setattr(cs.audit, "log_in",
                        lambda cur, rid, actor, name, action, details=None:
                        st["audit"].append((action, details)))

    def go(user, origin, tier):
        prep = cs.Prepared(
            user_id=user, user_name="Ex", target=PG, database="ledger", query=READ,
            required_mode=tier, justification="a month-end check", wants_result=True,
            result_format="csv", sched_for=None, explain_plan=None, risk_summary=None,
            origin=origin)
        out = cs.create_request(prep)
        assert isinstance(out, cs.Outcome), out
        return out
    st["go"] = go
    return st


def _submitted(st):
    return dict(st["audit"])["submitted"]


@pytest.mark.parametrize("tier", ["rw", "ddl"])
def test_a_super_admin_mcp_write_needs_approval(create, tier):
    create["super"].add(SUPER)
    out = create["go"](SUPER, "mcp", tier)
    assert out.auto_approved is False and out.aa_grant is None and out.fp_hit is None
    assert _submitted(create)["human_approval_forced"] is True


def test_a_super_admin_mcp_read_follows_the_normal_rule(create):
    create["super"].add(SUPER)
    out = create["go"](SUPER, "mcp", "ro")
    assert out.auto_approved is True
    assert "human_approval_forced" not in _submitted(create)


@pytest.mark.parametrize("tier", ["rw", "ddl"])
def test_a_waiver_does_not_cover_an_mcp_write(create, tier):
    create["waiver"] = True
    out = create["go"](ME, "mcp", tier)
    assert out.auto_approved is False and out.aa_grant is None
    assert create["decided"] == 0, "the waiver is not even consulted"


def test_a_waiver_still_covers_an_mcp_read(create):
    create["waiver"] = True
    out = create["go"](ME, "mcp", "ro")
    assert out.auto_approved is True and out.aa_grant["id"] == 501


def test_an_mcp_read_can_use_the_fingerprint_cache(create):
    create["prior"] = {"id": 77, "completed_at": None}
    out = create["go"](ME, "mcp", "ro")
    assert out.auto_approved is True and out.fp_hit["id"] == 77


@pytest.mark.parametrize("tier", ["", "superuser", "RO", None])
def test_an_unknown_tier_from_mcp_waits_for_a_person(create, tier):
    """Only the exact string `ro` is a read. Anything else is a write."""
    create["super"].add(SUPER)
    create["waiver"] = True
    assert create["go"](SUPER, "mcp", tier).auto_approved is False


@pytest.mark.parametrize("origin", ["slack", "web", "idp"])
@pytest.mark.parametrize("tier", ["ro", "rw", "ddl"])
def test_slack_web_and_idp_do_not_change(create, origin, tier):
    """Regression: a super-admin and a waiver holder still skip review."""
    create["super"].add(SUPER)
    assert create["go"](SUPER, origin, tier).auto_approved is True
    create["super"].clear()
    create["waiver"] = True
    assert create["go"](ME, origin, tier).auto_approved is True
    assert "human_approval_forced" not in _submitted(create)


# ---------------------------------------------------------------------------
# the server decides, whether or not the caller asked first
# ---------------------------------------------------------------------------

@pytest.fixture
def door(monkeypatch):
    st = {"validated": 0}
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools, "_target_or_refuse", lambda uid, c: PG)
    monkeypatch.setattr(tools.caller, "display_name", lambda uid: "Ex")
    monkeypatch.setattr(policy, "max_tier", lambda: "ro")

    def validate(*a, **k):
        st["validated"] += 1
        return cs.Rejection("query", "stop here")
    monkeypatch.setattr(cs, "validate_submission", validate)
    monkeypatch.setattr(tools.core_submit, "validate_submission", validate)

    def classify(*a, **k):
        raise AssertionError("classify_sql was called")
    monkeypatch.setattr(tools, "classify_sql", classify)
    st["submit"] = lambda sql: tools.submit_query(PG.alias, database="ledger", sql=sql, justification="a check")
    return st


@pytest.mark.parametrize("sql", ["UPDATE t SET a = 1", "DELETE FROM t"])
def test_submit_refuses_a_blocked_statement_without_classify(door, sql):
    with pytest.raises(tools.ToolError) as e:
        door["submit"](sql)
    assert e.value.code == "blocked" and door["validated"] == 0


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)", "UPDATE t SET a = 1 WHERE id = 2",
    "CREATE TABLE t (id int)", "SELECT nextval('s')",
], ids=["insert", "update", "create", "nextval"])
def test_submit_refuses_a_statement_over_the_ceiling_without_classify(door, sql):
    with pytest.raises(tools.ToolError) as e:
        door["submit"](sql)
    assert e.value.code == "tier_refused" and door["validated"] == 0


def test_explain_of_a_sequence_write_stays_read_only(door):
    """`EXPLAIN` runs nothing, so it passes the read-only ceiling and reaches
    the grant check, which is the stub here."""
    with pytest.raises(tools.ToolError) as e:
        door["submit"]("EXPLAIN SELECT nextval('s')")
    assert e.value.code == "query" and door["validated"] == 1


def test_a_plain_read_reaches_the_grant_check(door):
    with pytest.raises(tools.ToolError) as e:
        door["submit"](READ)
    assert e.value.code == "query" and door["validated"] == 1
