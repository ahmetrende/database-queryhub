"""What the web tells a super-admin about replicas, and where a query ran.

- `GET /queries/:id` carries `ranOn` -- {kind, name, forced, lagSeconds} --
  null until the request has run, and every `GET /history` row carries the
  same object from the same builder. `forced` comes from the audit row the
  executor writes when it HONOURED a choice, not from the stored intent. An
  automatic replica is never named; a chosen one is.
- The server writes NO "ran on" line for a chosen node -- the web client
  builds that sentence from `ranOn`, so a server copy would show twice -- and
  the automatic replica line is exactly what it was.
- `GET /connections` lists each connection's enabled replicas, for
  super-admins only.
- `GET /connections/:id/replicas` gives their health as automatic routing
  sees it, never raising for a replica that does not answer.
"""
from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from queryhub import executor, replicas, targets
from queryhub.web import app as web_app
from queryhub.web import deps, mapping, routes_data, routes_queries

SUPER = "U0EXAMPLE001"
PLAIN = "U0EXAMPLE002"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
REPLICA_ROW = {"id": 99, "alias": "prod-ledger-read-1", "host": "replica.example.test",
               "port": 5432}


def _alias_of(tid):
    return {7: "prod-ledger", 99: "prod-ledger-read-1"}.get(tid)


def _row(**kw):
    row = {"id": 1, "target_server_id": 7, "executed_at": NOW, "engine": "postgres",
           "executed_tier": "ro", "executed_target_id": None, "run_on": None}
    row.update(kw)
    return row


def _audit(action, details):
    return {"action": action, "details": details}


STARTED_ON_REPLICA = _audit("execution_started",
                            {"mode": "ro", "replica": "prod-ledger-read-1",
                             "replica_lag_s": 0.44})


# --- ranOn ---------------------------------------------------------------------------

def test_null_until_it_has_run():
    assert mapping.ran_on(_row(executed_at=None), [], _alias_of) is None


def test_auto_on_the_primary():
    assert mapping.ran_on(_row(), [_audit("execution_started", {"mode": "ro"})],
                          _alias_of) == {"kind": "primary", "name": "prod-ledger",
                                         "forced": False, "lagSeconds": None}


def test_auto_on_a_replica_does_not_name_it():
    got = mapping.ran_on(_row(executed_target_id=99), [STARTED_ON_REPLICA], _alias_of)
    assert got == {"kind": "replica", "name": "prod-ledger", "forced": False,
                   "lagSeconds": 0.4}


def test_auto_that_fell_back_reads_as_the_primary():
    """The fallback clears executed_target_id; where it finally ran is what
    counts, not where it was first sent."""
    got = mapping.ran_on(_row(executed_target_id=None),
                         [STARTED_ON_REPLICA, _audit("replica_fallback", {})], _alias_of)
    assert got["kind"] == "primary" and got["lagSeconds"] is None


def test_a_chosen_primary():
    forced = _audit("execution_run_on_forced",
                    {"requested": "primary", "ran_on": "primary", "target_id": 7,
                     "target": "prod-ledger", "lag_s": None})
    assert mapping.ran_on(_row(run_on="primary"), [forced], _alias_of) == {
        "kind": "primary", "name": "prod-ledger", "forced": True, "lagSeconds": None}


def test_a_chosen_replica_is_named_with_its_lag():
    forced = _audit("execution_run_on_forced",
                    {"requested": "replica:99", "ran_on": "replica", "target_id": 99,
                     "target": "prod-ledger-read-1", "lag_s": 45.0})
    got = mapping.ran_on(_row(run_on="replica:99", executed_target_id=99),
                         [STARTED_ON_REPLICA, forced], _alias_of)
    assert got == {"kind": "replica", "name": "prod-ledger-read-1", "forced": True,
                   "lagSeconds": 45.0}


def test_a_choice_that_was_not_honoured_is_not_forced():
    """The column says replica:99; the requester had lost super-admin by run
    time, so no forced row was written and the run was automatic."""
    got = mapping.ran_on(_row(run_on="replica:99", executed_target_id=99),
                         [STARTED_ON_REPLICA], _alias_of)
    assert got["forced"] is False and got["name"] == "prod-ledger"


def test_details_stored_as_text_are_read_too():
    forced = _audit("execution_run_on_forced",
                    json.dumps({"ran_on": "replica", "target": "prod-ledger-read-1",
                                "lag_s": 1.25}))
    got = mapping.ran_on(_row(executed_target_id=99), [forced], _alias_of)
    assert got["forced"] and got["lagSeconds"] == 1.2


def test_sql_server_reads_nobody_forced_are_not_guessed():
    """The executor sends those to the availability group's readable
    secondary and does not record which node served them."""
    assert mapping.ran_on(_row(engine="mssql"), [], _alias_of) is None
    assert mapping.ran_on(_row(engine="mssql", executed_tier="rw"), [],
                          _alias_of)["kind"] == "primary"
    forced = _audit("execution_run_on_forced", {"ran_on": "primary", "target": "legacy-sql"})
    assert mapping.ran_on(_row(engine="mssql"), [forced], _alias_of)["forced"] is True


# --- the Messages tab and the Slack result ---------------------------------------------

def _messages(notes):
    return [m["text"] for m in mapping._run_note_messages({"run_notes": notes}, "t")]


def test_a_chosen_run_has_no_server_line():
    """The executor passes no note for a chosen node (test_run_on_execution
    pins that), so the run notes hold none and the Messages tab adds none."""
    notes = executor._run_notes([])
    assert "replica" not in notes and "ran_on" not in notes
    assert _messages(notes) == []


def test_automatic_routing_keeps_its_unnamed_line():
    notes = executor._run_notes([], replica={"lag_s": 2.3})
    assert _messages(notes) == ["Ran on a read replica, about 2.3 s behind the primary."]
    assert "Ran on a read replica" in executor._replica_hint({"lag_s": 0.4})


def test_nothing_on_the_server_says_chosen():
    """No server-side sentence for a forced run anywhere: not the Messages
    builder, not the Slack hint, not the replica notes."""
    import inspect
    for fn in (mapping._run_note_messages, executor._replica_hint,
               executor._run_notes):
        assert "(chosen)" not in inspect.getsource(fn), fn.__name__
    assert not hasattr(replicas, "chosen_note")


# --- the HTTP surface ------------------------------------------------------------------

@pytest.fixture
def web(monkeypatch):
    logging.disable(logging.CRITICAL)
    state = {"uid": SUPER, "super": True}
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": state["uid"], "provider": "slack", "sid": "s1", "name": "Ex"}
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(routes_data.admins, "is_super_admin",
                        lambda uid: state["super"])
    c = TestClient(app)
    c.state = state
    yield c
    logging.disable(logging.NOTSET)


def test_the_status_endpoint_carries_ran_on(web, monkeypatch):
    row = _row(id=5, requester_slack_id=SUPER, status="completed", query="SELECT 1",
               created_at=NOW, completed_at=NOW, decided_at=NOW,
               decided_by_slack_id=SUPER, decided_by_name="Ex (super-admin)",
               run_on="replica:99", executed_target_id=99,
               run_notes={"statements": [{"i": 1, "leading": "SELECT"}], "notices": [],
                          "truncated": False,
                          "ran_on": {"forced": True, "kind": "replica",
                                     "name": "prod-ledger-read-1", "lag_s": 0.4}})
    audits = [
        {"actor_slack_id": SUPER, "actor_name": "Ex", "action": "submitted",
         "created_at": NOW, "details": {}},
        {"actor_slack_id": None, "actor_name": None, "action": "execution_started",
         "created_at": NOW, "details": {"replica": "prod-ledger-read-1",
                                        "replica_lag_s": 0.4}},
        {"actor_slack_id": SUPER, "actor_name": "Ex", "action": "execution_run_on_forced",
         "created_at": NOW, "details": {"requested": "replica:99", "ran_on": "replica",
                                        "target_id": 99, "target": "prod-ledger-read-1",
                                        "lag_s": 0.4}},
    ]
    seen = []
    monkeypatch.setattr(routes_queries.db, "fetch_one", lambda sql, p=None: dict(row))
    monkeypatch.setattr(routes_queries.db, "fetch_all",
                        lambda sql, p=None: seen.append(sql) or [dict(a) for a in audits])
    monkeypatch.setattr(routes_queries, "_alias_of", _alias_of)
    monkeypatch.setattr(routes_queries.people, "display_name", lambda *a, **k: "Ex")
    r = web.get("/api/queries/5")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ranOn"] == {"kind": "replica", "name": "prod-ledger-read-1",
                             "forced": True, "lagSeconds": 0.4}
    texts = [m["text"] for m in body["messages"]]
    assert not any("Ran on" in t for t in texts), texts
    assert len(seen) == 1 and "details" in seen[0], "ranOn cost a second audit read"


def test_ran_on_is_null_before_the_run_on_the_status_poll(web, monkeypatch):
    row = _row(id=6, requester_slack_id=SUPER, status="pending", query="SELECT 1",
               created_at=NOW, executed_at=None, run_on="replica:99")
    monkeypatch.setattr(routes_queries.db, "fetch_one", lambda sql, p=None: dict(row))
    monkeypatch.setattr(routes_queries.db, "fetch_all", lambda sql, p=None: [])
    monkeypatch.setattr(routes_queries.people, "display_name", lambda *a, **k: "Ex")
    body = web.get("/api/queries/6").json()
    assert "ranOn" in body and body["ranOn"] is None


# --- GET /history ------------------------------------------------------------------------

def _history_env(monkeypatch, rows, audits):
    seen = {"sql": [], "aliases": []}

    def fetch_all(sql, p=None):
        flat = " ".join(sql.split())
        seen["sql"].append((flat, p))
        if flat.startswith("SELECT id, query, target_server_id"):
            return [dict(r) for r in rows]
        if "FROM audit_log" in flat:
            return [dict(a) for a in audits if a["request_id"] in p[0]]
        raise AssertionError(f"unexpected read: {flat}")
    monkeypatch.setattr(routes_data.db, "fetch_all", fetch_all)
    monkeypatch.setattr(routes_data, "_alias_of",
                        lambda tid: seen["aliases"].append(tid) or _alias_of(tid))
    monkeypatch.setattr(routes_data, "_conn_state_resolver", lambda uid: lambda tid: "ok")
    monkeypatch.setattr(routes_data.people, "namer", lambda names: lambda n: n)
    return seen


def _hrow(rid, **kw):
    row = {"id": rid, "query": "SELECT 1", "target_server_id": 7,
           "database_name": "ledger", "status": "completed", "row_count": 1,
           "created_at": NOW, "decided_by_slack_id": SUPER,
           "decided_by_name": "Ex", "executed_at": NOW, "executed_target_id": None,
           "executed_tier": "ro", "engine": "postgres"}
    row.update(kw)
    return row


def test_history_rows_carry_the_same_ran_on_as_the_status_poll(web, monkeypatch):
    forced = {"request_id": 3, "action": "execution_run_on_forced",
              "details": {"requested": "replica:99", "ran_on": "replica",
                          "target_id": 99, "target": "prod-ledger-read-1",
                          "lag_s": 45.0}}
    started_auto = {"request_id": 2, "action": "execution_started",
                    "details": {"replica": "prod-ledger-read-1", "replica_lag_s": 0.4}}
    rows = [_hrow(4, status="pending", executed_at=None),
            _hrow(3, executed_target_id=99),
            _hrow(2, executed_target_id=99),
            _hrow(1)]
    _history_env(monkeypatch, rows, [forced, started_auto])
    r = web.get("/api/history")
    assert r.status_code == 200, r.text
    got = {h["id"]: h["ranOn"] for h in r.json()["history"]}
    assert got == {
        "4": None,
        "3": {"kind": "replica", "name": "prod-ledger-read-1", "forced": True,
              "lagSeconds": 45.0},
        "2": {"kind": "replica", "name": "prod-ledger", "forced": False,
              "lagSeconds": 0.4},
        "1": {"kind": "primary", "name": "prod-ledger", "forced": False,
              "lagSeconds": None},
    }
    # Each row's ranOn is exactly what the status poll's builder says.
    for row in rows:
        mine = [a for a in (forced, started_auto) if a["request_id"] == row["id"]]
        assert got[str(row["id"])] == mapping.ran_on(row, mine, _alias_of)


def test_history_reads_the_audit_rows_once_and_only_for_runs(web, monkeypatch):
    rows = [_hrow(5, status="pending", executed_at=None), _hrow(4), _hrow(3)]
    seen = _history_env(monkeypatch, rows, [])
    web.get("/api/history")
    audit_reads = [(sql, p) for sql, p in seen["sql"] if "FROM audit_log" in sql]
    assert len(audit_reads) == 1
    sql, params = audit_reads[0]
    assert "request_id = ANY(%s)" in sql and params == ([4, 3],)
    assert "'execution_started', 'execution_run_on_forced'" in sql
    # One alias lookup for the page, not one per row and field.
    assert seen["aliases"] == [7]


def test_history_of_nothing_run_asks_the_audit_log_nothing(web, monkeypatch):
    seen = _history_env(monkeypatch, [_hrow(9, status="pending", executed_at=None)], [])
    body = web.get("/api/history").json()
    assert body["history"][0]["ranOn"] is None
    assert not any("FROM audit_log" in sql for sql, _p in seen["sql"])


def _connections_env(monkeypatch, fleet):
    monkeypatch.setattr(routes_data.targets, "list_all", lambda: fleet)
    monkeypatch.setattr(routes_data.targets, "list_enabled",
                        lambda: [t for t in fleet if t.replica_of is None])
    monkeypatch.setattr(routes_data.db, "fetch_all", lambda sql, p=None: [])
    monkeypatch.setattr(routes_data.teams, "effective_grants_for_user",
                        lambda uid, ids: {i: {"allowed_databases": None, "mode": "ddl"}
                                          for i in ids})
    monkeypatch.setattr(routes_data, "_catalog_databases_map",
                        lambda ids: {i: ["ledger"] for i in ids})
    monkeypatch.setattr(routes_data.auto_approve, "active_grants", lambda uid: [])
    monkeypatch.setattr(routes_data, "_catalog_table_refs_map", lambda pairs: {})
    monkeypatch.setattr(routes_data, "_catalog_functions_map", lambda pairs: {})
    asked = []

    def by_primary(ids):
        asked.append(sorted(ids))
        return {7: [{"id": 99, "alias": "prod-ledger-read-1"}]}
    monkeypatch.setattr(routes_data.replicas, "enabled_by_primary", by_primary)
    return asked


def _t(tid, alias, engine="postgres", replica_of=None):
    return targets.TargetServer(id=tid, alias=alias, host=f"{alias}.example.test",
                                port=5432, default_database="ledger", username="u",
                                enabled=True, notes=None, engine=engine,
                                replica_of=replica_of)


FLEET = [_t(7, "prod-ledger"), _t(99, "prod-ledger-read-1", replica_of=7),
         _t(8, "events", engine="clickhouse")]


def test_connections_list_replicas_for_a_super_admin(web, monkeypatch):
    asked = _connections_env(monkeypatch, FLEET)
    r = web.get("/api/connections")
    assert r.status_code == 200, r.text
    conns = {c["id"]: c for c in r.json()["connections"]}
    assert set(conns) == {"prod-ledger", "events"}, "a replica became a connection"
    assert conns["prod-ledger"]["replicas"] == [{"id": "99", "name": "prod-ledger-read-1"}]
    assert conns["events"]["replicas"] == []          # not Postgres: none to choose
    assert asked == [[7]], "replicas were looked up for a non-Postgres connection"


def test_connections_show_nobody_else_a_replica(web, monkeypatch):
    web.state.update(uid=PLAIN, super=False)
    asked = _connections_env(monkeypatch, FLEET)
    conns = web.get("/api/connections").json()["connections"]
    assert conns and all("replicas" not in c for c in conns)
    assert asked == []


def test_enabled_by_primary_is_one_read_grouped_by_primary(monkeypatch):
    seen = []
    monkeypatch.setattr(replicas.db, "fetch_all", lambda sql, p=None: seen.append(
        (" ".join(sql.split()), p)) or [
        {"id": 99, "alias": "a", "replica_of": 7}, {"id": 100, "alias": "b", "replica_of": 7},
        {"id": 54, "alias": "c", "replica_of": 53}])
    got = replicas.enabled_by_primary([7, 53, 7])
    assert got == {7: [{"id": 99, "alias": "a"}, {"id": 100, "alias": "b"}],
                   53: [{"id": 54, "alias": "c"}]}
    (sql, params), = seen
    assert "replica_of = ANY(%s) AND enabled" in sql and params == ([7, 53],)
    monkeypatch.setattr(replicas.db, "fetch_all", lambda *a: pytest.fail("asked for nothing"))
    assert replicas.enabled_by_primary([]) == {}


# --- GET /connections/:id/replicas -----------------------------------------------------

@pytest.fixture
def replicas_env(monkeypatch):
    replicas._health.clear()
    env = {"probes": [], "answers": {"prod-ledger.example.test": ("3B4F/FA6EE000",),
                                     "replica.example.test": (True, 8192, 2.26)},
           "creds": ("reader", "pw")}
    monkeypatch.setattr(routes_data, "_target_by_alias",
                        lambda a: {"prod-ledger": FLEET[0], "prod-ledger-read-1": FLEET[1],
                                   "events": FLEET[2]}.get(a))
    monkeypatch.setattr(replicas, "replicas_of",
                        lambda pid: [REPLICA_ROW] if pid == 7 else [])

    def creds(tid, mode):
        assert mode == "ro", "health must be read with the read-only login"
        if isinstance(env["creds"], Exception):
            raise env["creds"]
        return env["creds"]
    monkeypatch.setattr(routes_data.targets, "get_credentials", creds)
    monkeypatch.setattr(replicas.cfg, "get_int", lambda k, d=None: d)
    monkeypatch.setattr(replicas.cfg, "target_ssl_kwargs", lambda host=None: {})
    monkeypatch.setattr(replicas.log, "info", lambda *a, **k: None)

    @contextlib.contextmanager
    def connect(host=None, port=None, **kw):
        env["probes"].append((host, kw.get("user")))
        answer = env["answers"][host]
        if isinstance(answer, Exception):
            raise answer

        class Cur:
            def execute(self, sql, params=None):
                pass

            def fetchone(self):
                return answer

        class Conn:
            def cursor(self):
                @contextlib.contextmanager
                def c():
                    yield Cur()
                return c()
        yield Conn()
    monkeypatch.setattr(psycopg, "connect", connect)
    yield env
    replicas._health.clear()


def test_the_replicas_endpoint_is_super_admin_only(web, replicas_env):
    web.state.update(uid=PLAIN, super=False)
    r = web.get("/api/connections/prod-ledger/replicas")
    assert r.status_code == 403
    assert replicas_env["probes"] == []


def test_an_unknown_connection_or_a_replica_alias_is_404(web, replicas_env):
    assert web.get("/api/connections/nope/replicas").status_code == 404
    assert web.get("/api/connections/prod-ledger-read-1/replicas").status_code == 404


def test_a_healthy_replica_reports_its_lag(web, replicas_env):
    r = web.get("/api/connections/prod-ledger/replicas")
    assert r.status_code == 200, r.text
    assert r.json() == {"replicas": [{"id": "99", "name": "prod-ledger-read-1",
                                      "healthy": True, "lagSeconds": 2.3,
                                      "reason": None}]}
    # The primary's read-only login, primary first -- the way routing measures.
    assert replicas_env["probes"] == [("prod-ledger.example.test", "reader"),
                                      ("replica.example.test", "reader")]


def test_the_cached_health_is_used(web, replicas_env):
    web.get("/api/connections/prod-ledger/replicas")
    web.get("/api/connections/prod-ledger/replicas")
    assert len(replicas_env["probes"]) == 2, "the second call probed again"


def test_an_unreachable_replica_is_an_answer_not_an_error(web, replicas_env):
    replicas_env["answers"]["replica.example.test"] = psycopg.OperationalError("timeout")
    r = web.get("/api/connections/prod-ledger/replicas")
    assert r.status_code == 200, r.text
    (rep,) = r.json()["replicas"]
    assert rep["healthy"] is False and rep["lagSeconds"] is None
    assert rep["reason"] == "unreachable (OperationalError)"


def test_a_replica_past_the_limit_says_why_auto_passes_it_over(web, replicas_env):
    replicas_env["answers"]["replica.example.test"] = (True, 10 ** 9, 45.0)
    (rep,) = web.get("/api/connections/prod-ledger/replicas").json()["replicas"]
    assert rep == {"id": "99", "name": "prod-ledger-read-1", "healthy": False,
                   "lagSeconds": 45.0, "reason": "45s behind (limit 10s)"}


def test_no_read_only_login_is_an_answer_too(web, replicas_env):
    replicas_env["creds"] = LookupError("no ro creds")
    (rep,) = web.get("/api/connections/prod-ledger/replicas").json()["replicas"]
    assert rep["healthy"] is False and "read-only login" in rep["reason"]
    assert replicas_env["probes"] == []


def test_a_check_that_raises_is_an_answer_too(web, replicas_env, monkeypatch):
    monkeypatch.setattr(routes_data.replicas, "health",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("config read")))
    (rep,) = web.get("/api/connections/prod-ledger/replicas").json()["replicas"]
    assert rep["healthy"] is False and rep["reason"] == "could not be checked (RuntimeError)"


def test_a_connection_without_replicas_or_not_postgres_has_none(web, replicas_env):
    assert web.get("/api/connections/events/replicas").json() == {"replicas": []}
    assert replicas_env["probes"] == []
