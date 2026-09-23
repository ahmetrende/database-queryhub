"""A read-only request runs on a healthy read replica of its target.

The operator's rule (2026-09-23): if a request is RO and its database has a
healthy replica, the replica runs it -- and nobody picks a replica: every list
shows the one name it always showed. What the tests below pin is the part of
that rule that is not obvious from its wording:

- "healthy" is measured against the primary's WAL position, not by the age of
  the last replayed commit, which reads a quiet primary as a lagging replica;
- some reads describe the server they run on (pg_stat_activity, pg_locks) and
  must stay on the primary, and so must a person's own read-after-write;
- a replica that fails the query for a reason of its own hands it to the
  primary, once -- but a timeout or a user's cancel is never re-run;
- the cancel reaches the server that runs the query, with the primary's login;
- the requester is told, without the replica's name.
"""
import contextlib
import inspect
import sys
from pathlib import Path

import psycopg
import pytest

from queryhub import (access, cancellation, core_submit, executor, query_safety,
                           replicas, targets, teams)
from queryhub.web import mapping, routes_admin as ra

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PRIMARY = type("T", (), {"id": 7, "alias": "prod-ledger", "engine": "postgres",
                         "host": "primary.example.test", "port": 5432,
                         "default_database": "ledger", "enabled": True,
                         "username": "reader", "replica_of": None})()
REPLICA_ROW = {"id": 99, "alias": "prod-ledger-read-1",
               "host": "replica.example.test", "port": 5432}
REQUEST = {"id": 1, "query": "SELECT * FROM orders", "requester_slack_id": "U0EXAMPLE001"}


@pytest.fixture(autouse=True)
def _fresh_health_cache():
    replicas._health.clear()
    yield
    replicas._health.clear()


@pytest.fixture
def on(monkeypatch):
    """Routing on, one enabled replica, no recent write."""
    settings = {"replica_routing": "on"}
    monkeypatch.setattr(replicas.cfg, "get_setting",
                        lambda k, d=None: settings.get(k, d))
    monkeypatch.setattr(replicas.cfg, "get_int", lambda k, d=None: d)
    monkeypatch.setattr(replicas, "replicas_of", lambda pid: [REPLICA_ROW])
    monkeypatch.setattr(replicas, "_wrote_recently", lambda *a: False)
    return settings


def _healthy(monkeypatch, lag=0.4):
    monkeypatch.setattr(replicas, "_probe",
                        lambda p, r, u, pw: replicas.Health(True, lag))


# --- who is routed at all ----------------------------------------------------------

@pytest.mark.parametrize("engine,mode", [("postgres", "rw"), ("postgres", "ddl"),
                                         ("mssql", "ro"), ("clickhouse", "ro")])
def test_only_a_postgres_read_is_ever_routed(monkeypatch, engine, mode):
    def boom(*a, **k):
        raise AssertionError("looked for replicas of a request that cannot use one")
    monkeypatch.setattr(replicas, "replicas_of", boom)
    monkeypatch.setattr(replicas.cfg, "get_setting", boom)
    t = type("T", (), {"id": 7, "engine": engine})()
    assert replicas.choose(t, REQUEST, mode, "u", "p") == replicas.Decision()


def test_routing_is_off_until_the_switch_says_on(monkeypatch, on):
    on["replica_routing"] = "off"
    monkeypatch.setattr(replicas, "replicas_of", lambda pid: pytest.fail("read replicas"))
    assert replicas.choose(PRIMARY, REQUEST, "ro", "u", "p") == replicas.Decision()


def test_a_target_without_a_replica_says_nothing(monkeypatch, on):
    monkeypatch.setattr(replicas, "replicas_of", lambda pid: [])
    assert replicas.choose(PRIMARY, REQUEST, "ro", "u", "p") == replicas.Decision()


def test_a_healthy_replica_runs_the_read(monkeypatch, on):
    _healthy(monkeypatch, lag=0.44)
    d = replicas.choose(PRIMARY, REQUEST, "ro", "u", "p")
    assert d.route == replicas.Route(99, "prod-ledger-read-1", "replica.example.test",
                                     5432, 0.4)
    assert d.skipped is None


def test_an_unhealthy_replica_leaves_it_on_the_primary_and_says_why(monkeypatch, on):
    monkeypatch.setattr(replicas, "_probe",
                        lambda p, r, u, pw: replicas.Health(False, 42.0, "42s behind (limit 10s)"))
    d = replicas.choose(PRIMARY, REQUEST, "ro", "u", "p")
    assert d.route is None and "42s behind" in d.skipped


@pytest.mark.parametrize("sql,name", [
    ("SELECT pid, state FROM pg_stat_activity", "pg_stat_activity"),
    ("select * from PG_LOCKS l join pg_class c on c.oid = l.relation", "PG_LOCKS"),
    ("SELECT pg_current_wal_lsn()", "pg_current_wal_lsn"),
    ("SELECT txid_current()", "txid_current"),
])
def test_a_read_about_the_server_itself_stays_on_the_primary(monkeypatch, on, sql, name):
    _healthy(monkeypatch)
    d = replicas.choose(PRIMARY, {**REQUEST, "query": sql}, "ro", "u", "p")
    assert d.route is None and name in d.skipped


def test_a_node_local_name_in_a_comment_or_a_string_does_not_count():
    assert replicas.node_local("SELECT 'pg_stat_activity' AS note -- pg_locks") is None
    assert replicas.node_local("SELECT * FROM pg_stat_activity") == "pg_stat_activity"


def test_the_requesters_own_recent_write_keeps_their_reads_on_the_primary(monkeypatch, on):
    _healthy(monkeypatch)
    monkeypatch.setattr(replicas, "_wrote_recently", lambda *a: True)
    d = replicas.choose(PRIMARY, REQUEST, "ro", "u", "p")
    assert d.route is None and "wrote to it" in d.skipped


def test_read_your_writes_can_be_switched_off(monkeypatch, on):
    _healthy(monkeypatch)
    monkeypatch.setattr(replicas.cfg, "get_int",
                        lambda k, d=None: 0 if k == "replica_read_your_writes_minutes" else d)
    monkeypatch.setattr(replicas, "_wrote_recently",
                        lambda *a: pytest.fail("checked recent writes while switched off"))
    assert replicas.choose(PRIMARY, REQUEST, "ro", "u", "p").route is not None


def test_the_recent_write_check_compares_the_tier_as_text():
    src = inspect.getsource(replicas._wrote_recently)
    assert "executed_tier IN ('rw', 'ddl')" in src and "executed_at >" in src


def test_a_failure_to_decide_sends_it_to_the_primary(monkeypatch, on):
    def broken(pid):
        raise RuntimeError("bot DB blinked")
    monkeypatch.setattr(replicas, "replicas_of", broken)
    monkeypatch.setattr(replicas.log, "exception", lambda *a, **k: None)
    assert replicas.choose(PRIMARY, REQUEST, "ro", "u", "p") == replicas.Decision()


# --- what "healthy" means ----------------------------------------------------------

def _fake_connect(results, calls):
    """psycopg.connect stand-in: the primary answers its LSN, the replica its state."""
    @contextlib.contextmanager
    def connect(host=None, port=None, **kw):
        calls.append(host)
        if isinstance(results.get(host), Exception):
            raise results[host]

        class Cur:
            def execute(self, sql, params=None):
                self.sql = sql

            def fetchone(self):
                return results[host]

        class Conn:
            def cursor(self):
                @contextlib.contextmanager
                def c():
                    yield Cur()
                return c()
        yield Conn()
    return connect


def _probe_with(monkeypatch, replica_answer, primary_answer=("3B4F/FA6EE000",)):
    calls = []
    monkeypatch.setattr(replicas.cfg, "target_ssl_kwargs", lambda: {})
    monkeypatch.setattr(replicas.cfg, "get_int", lambda k, d=None: d)
    monkeypatch.setattr(replicas.log, "info", lambda *a, **k: None)
    monkeypatch.setattr(replicas.psycopg, "connect", _fake_connect(
        {"primary.example.test": primary_answer,
         "replica.example.test": replica_answer}, calls))
    return replicas._probe(PRIMARY, REPLICA_ROW, "u", "p"), calls


def test_a_replica_past_the_primarys_position_is_caught_up_whatever_its_replay_age(monkeypatch):
    """A quiet primary commits nothing, so the last replayed commit ages while
    the replica is fully caught up. Reading age alone would call it lagging."""
    h, calls = _probe_with(monkeypatch, (True, -950224, 600.0))
    assert h == replicas.Health(True, 0.0)
    assert calls == ["primary.example.test", "replica.example.test"]   # primary FIRST


def test_a_replica_behind_the_primary_is_as_old_as_its_last_replayed_commit(monkeypatch):
    h, _ = _probe_with(monkeypatch, (True, 8192, 2.3))
    assert h.ok and h.lag_s == 2.3


def test_a_replica_too_far_behind_is_not_used(monkeypatch):
    h, _ = _probe_with(monkeypatch, (True, 10 ** 9, 45.0))
    assert not h.ok and "45s behind (limit 10s)" == h.reason


def test_a_promoted_replica_is_not_used(monkeypatch):
    h, _ = _probe_with(monkeypatch, (False, None, None))
    assert not h.ok and h.reason == "not in recovery"


def test_an_unreachable_replica_is_not_used(monkeypatch):
    h, _ = _probe_with(monkeypatch, psycopg.OperationalError("timeout expired"))
    assert not h.ok and "unreachable" in h.reason


def test_one_check_is_trusted_for_its_ttl_and_a_failure_overrides_it(monkeypatch):
    n = []
    monkeypatch.setattr(replicas.cfg, "get_int", lambda k, d=None: d)
    monkeypatch.setattr(replicas, "_probe",
                        lambda *a: n.append(1) or replicas.Health(True, 0.1))
    assert replicas.health(PRIMARY, REPLICA_ROW, "u", "p").ok
    assert replicas.health(PRIMARY, REPLICA_ROW, "u", "p").ok
    assert len(n) == 1
    replicas.mark_unhealthy(99, "SerializationFailure")
    assert not replicas.health(PRIMARY, REPLICA_ROW, "u", "p").ok and len(n) == 1


# --- which failures hand the query to the primary -----------------------------------

@pytest.mark.parametrize("error,retry", [
    (psycopg.errors.SerializationFailure(
        "canceling statement due to conflict with recovery"), True),
    (psycopg.errors.SerializationFailure("could not serialize access"), False),
    (psycopg.errors.AdminShutdown("terminating connection due to administrator command"), True),
    (psycopg.OperationalError("connection to server failed: timeout expired"), True),
    (psycopg.errors.QueryCanceled("canceling statement due to statement timeout"), False),
    (psycopg.errors.QueryCanceled("canceling statement due to user request"), False),
    (psycopg.errors.LockNotAvailable("could not obtain lock"), False),
])
def test_only_a_failure_of_the_replica_itself_is_retried(error, retry):
    assert replicas.is_fallback_error(error) is retry


# --- the executor ------------------------------------------------------------------

class _Result:
    csv_path = None
    notices: list = []


@pytest.fixture
def run_env(monkeypatch):
    """executor._run down the Postgres path, with every collaborator faked. The
    connection factory records the host of each attempt; a test decides what the
    replica does by setting box["replica_error"]."""
    box = {"hosts": [], "claims": [], "audits": [], "updates": [], "finalized": [],
           "failed": [], "replica_error": None, "unhealthy": [],
           "route": replicas.Route(99, "prod-ledger-read-1", "replica.example.test",
                                   5432, 0.4)}
    report = query_safety.SafetyReport(main_tier="ro", statements=[
        query_safety.StatementInfo(raw="SELECT 1", rewritten="SELECT 1", kind="ro",
                                   leading="SELECT")])
    monkeypatch.setattr(executor.query_secrets, "statement_to_run", lambda r: r["query"])
    monkeypatch.setattr(executor.targets, "get", lambda tid: PRIMARY)
    monkeypatch.setattr(executor.engines, "is_executable", lambda e: True)
    monkeypatch.setattr(executor.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(executor.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(executor.query_safety, "analyze", lambda *a, **k: report)
    monkeypatch.setattr(executor.teams, "effective_mode_for_database", lambda *a: "ro")
    monkeypatch.setattr(executor.targets, "get_credentials", lambda tid, mode: ("reader", "pw"))
    monkeypatch.setattr(executor.replicas, "choose",
                        lambda *a: replicas.Decision(route=box["route"]))
    monkeypatch.setattr(executor.replicas, "mark_unhealthy",
                        lambda rid, why: box["unhealthy"].append(rid))
    monkeypatch.setattr(executor.cfg, "get_int", lambda k, d=None: d)
    monkeypatch.setattr(executor.cfg, "get_setting", lambda k, d=None: d)
    monkeypatch.setattr(executor.cfg, "target_ssl_kwargs", lambda: {})
    monkeypatch.setattr(executor.row_limits, "effective_caps", lambda uid: (1000, 10 ** 6))
    monkeypatch.setattr(executor, "_super_role_for", lambda *a: None)
    monkeypatch.setattr(executor, "_team_role_for", lambda *a: None)
    monkeypatch.setattr(executor, "_cancel_requested", lambda rid: box.get("cancelled", False))
    monkeypatch.setattr(executor, "_build_application_name", lambda r: "queryhub-test")
    monkeypatch.setattr(executor.pg_types, "register_infinity_safe_loaders", lambda c: None)
    def execute_main(cur, *a, **k):
        # Where the statement itself runs -- the pid lookup before it is
        # best-effort and swallows its own errors, on purpose.
        if cur.on_replica and box["replica_error"] is not None:
            raise box["replica_error"]
        return _Result()
    monkeypatch.setattr(executor, "_execute_main_statement", execute_main)
    monkeypatch.setattr(executor, "_finalize",
                        lambda *a, **k: box["finalized"].append(k.get("replica")))
    monkeypatch.setattr(executor, "_fail",
                        lambda client, request, msg, **k: box["failed"].append(msg))
    monkeypatch.setattr(executor.audit, "log_in",
                        lambda cur, rid, a, n, action, details=None:
                        box["audits"].append((action, details)))

    class TxnCur:
        rowcount = 1

        def execute(self, sql, params=None):
            flat = " ".join(sql.split())
            (box["claims"] if "status = 'executing'" in flat else box["updates"]).append(
                (flat, params))

    @contextlib.contextmanager
    def txn():
        yield TxnCur()
    monkeypatch.setattr(executor.db, "transaction", txn)
    # The generic failure handler reads the row's status before failing it.
    monkeypatch.setattr(executor.db, "fetch_one",
                        lambda sql, params=None: {"status": "executing"})

    @contextlib.contextmanager
    def connect(host=None, port=None, **kw):
        box["hosts"].append(host)
        on_replica = host == "replica.example.test"

        class Cur:
            def __init__(self):
                self.on_replica = on_replica

            def execute(self, sql, params=None):
                pass

            def fetchone(self):
                return (4242,)

        class Conn:
            def add_notice_handler(self, h):
                pass

            def cursor(self):
                @contextlib.contextmanager
                def c():
                    yield Cur()
                return c()

            def commit(self):
                pass
        yield Conn()
    monkeypatch.setattr(executor.psycopg, "connect", connect)
    return box


def _run():
    executor._run({"id": 1, "query": "SELECT 1", "target_server_id": 7,
                   "database_name": "ledger", "wants_result": True,
                   "result_format": "csv", "requester_slack_id": "U0EXAMPLE001",
                   "engine": "postgres", "required_tier": "ro"}, None)


def test_a_routed_read_connects_to_the_replica_and_records_it(run_env):
    _run()
    assert run_env["hosts"] == ["replica.example.test"]
    claim_sql, claim_params = run_env["claims"][0]
    assert "executed_target_id = %s" in claim_sql and claim_params[1] == 99
    started = dict(run_env["audits"])["execution_started"]
    assert started["replica"] == "prod-ledger-read-1" and started["replica_lag_s"] == 0.4
    assert run_env["finalized"] == [{"lag_s": 0.4}]


def test_a_recovery_conflict_on_the_replica_reruns_it_on_the_primary(run_env):
    run_env["replica_error"] = psycopg.errors.SerializationFailure(
        "canceling statement due to conflict with recovery")
    _run()
    assert run_env["hosts"] == ["replica.example.test", "primary.example.test"]
    assert ("replica_fallback" in dict(run_env["audits"])
            and dict(run_env["audits"])["replica_fallback"]["replica"] == "prod-ledger-read-1")
    assert any("executed_target_id = NULL" in sql for sql, _ in run_env["updates"])
    assert run_env["unhealthy"] == [99]
    assert run_env["finalized"] == [None]        # it ran on the primary: nothing to tell
    assert run_env["failed"] == []


def test_a_timeout_on_the_replica_is_not_run_again(run_env):
    run_env["replica_error"] = psycopg.errors.QueryCanceled(
        "canceling statement due to statement timeout")
    _run()
    assert run_env["hosts"] == ["replica.example.test"]
    assert run_env["failed"] and "timeout" in run_env["failed"][0]


def test_a_query_its_owner_cancelled_is_not_run_again(run_env):
    run_env["replica_error"] = psycopg.errors.AdminShutdown("terminating connection")
    run_env["cancelled"] = True
    _run()
    assert run_env["hosts"] == ["replica.example.test"]
    assert run_env["failed"] and "replica_fallback" not in dict(run_env["audits"])


def test_an_unrouted_read_connects_to_the_primary(run_env):
    run_env["route"] = None
    _run()
    assert run_env["hosts"] == ["primary.example.test"]
    assert run_env["claims"][0][1][1] is None
    assert run_env["finalized"] == [None]


# --- the cancel --------------------------------------------------------------------

def test_a_cancel_reaches_the_replica_with_the_primarys_login(monkeypatch):
    row = {"backend_pid": 4242, "engine_execution_id": None, "target_server_id": 7,
           "executed_target_id": 99, "database_name": "ledger", "st": "executing"}
    monkeypatch.setattr(cancellation.db, "fetch_one", lambda sql, p=None: row)
    replica = type("T", (), {"id": 99, "host": "replica.example.test", "port": 5433,
                             "username": "placeholder", "engine": "postgres"})()
    monkeypatch.setattr(cancellation.targets, "get",
                        lambda tid: PRIMARY if tid == 7 else replica)
    monkeypatch.setattr(cancellation.targets, "get_password",
                        lambda tid: "primary-pw" if tid == 7 else "sentinel")
    monkeypatch.setattr(cancellation.cfg, "target_ssl_kwargs", lambda: {})
    monkeypatch.setattr(cancellation.cfg, "get_int", lambda k, d=None: 1)
    seen = []

    def connect(dsn, **kw):
        seen.append(dsn)
        raise psycopg.OperationalError("stop here")
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(cancellation.log, "exception", lambda *a, **k: None)
    cancellation.stop_backend(1)
    assert seen and "host=replica.example.test port=5433" in seen[0]
    assert "user=reader" in seen[0] and "password=primary-pw" in seen[0]


# --- one name everywhere -------------------------------------------------------------

def test_no_picker_lists_a_replica():
    for fn in (targets.list_enabled, targets.search):
        assert "replica_of IS NULL" in inspect.getsource(fn), fn.__name__
    for fn in (access.visible_targets, access.search_visible_targets,
               teams.list_targets_for_user, teams.search_targets_for_user):
        src = inspect.getsource(fn)
        # every branch: the admin one and the grant-scoped one
        assert src.count("replica_of IS NULL") >= 2, fn.__name__


def test_the_web_connection_list_leaves_replicas_out():
    from queryhub.web import routes_data
    assert 'getattr(t, "replica_of", None) is None' in inspect.getsource(routes_data)


def test_a_replica_cannot_be_submitted_to(monkeypatch):
    from queryhub import lifecycle
    replica = type("T", (), {"id": 99, "alias": "prod-ledger-read-1", "engine": "postgres",
                             "replica_of": 7, "enabled": True})()
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(core_submit, "kill_switch_on", lambda: False)
    monkeypatch.setattr(core_submit.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(core_submit.cfg, "get_int", lambda k, d=None: d)
    monkeypatch.setattr(core_submit.targets, "get",
                        lambda tid: replica if tid == 99 else PRIMARY)
    r = core_submit.validate_submission("U0EXAMPLE001", "Ex", target_server_id=99,
                                        database_name="ledger", query="SELECT 1",
                                        justification="check")
    assert isinstance(r, core_submit.Rejection) and r.field == "server"
    assert "`prod-ledger`" in r.message and "read replica" in r.message


def test_a_replica_can_be_enabled_without_a_credential_of_its_own():
    creds = {m: {"username": None, "configured": False, "placeholder": True}
             for m in ("ro", "rw", "ddl")}
    row = {"id": 99, "alias": "prod-ledger-read-1", "enabled": False, "engine": "postgres",
           "host": "h", "port": 5432, "default_database": "ledger", "notes": None,
           "tags": {}, "credentials": creds, "replica_of": 7}
    changes, _ = ra._plan_connection_update(row, ra.ConnectionPatch(enabled=True))
    assert changes == {"enabled": True}
    with pytest.raises(Exception):
        ra._plan_connection_update({**row, "replica_of": None},
                                   ra.ConnectionPatch(enabled=True))


def test_the_admin_registry_says_whose_replica_a_row_is():
    assert "replicaOf" in inspect.getsource(ra._connection_entry)
    assert "replica_of_alias" in inspect.getsource(targets)


# --- the requester is told -----------------------------------------------------------

def test_the_messages_tab_says_it_ran_on_a_replica_without_naming_it():
    lines = mapping._run_note_messages(
        {"run_notes": {"statements": [{"i": 1, "leading": "select"}],
                       "notices": [], "truncated": False,
                       "replica": {"lag_s": 2.3}}}, "10:00:00")
    texts = [x["text"] for x in lines]
    assert "Ran on a read replica, about 2.3 s behind the primary." in texts
    assert not any("read-1" in t for t in texts)


def test_a_caught_up_replica_says_so():
    assert replicas.served_note(0.0) == "Ran on a read replica, caught up with the primary."


def test_run_notes_carry_the_replica_only_when_one_ran_it():
    assert "replica" not in executor._run_notes([])
    assert executor._run_notes([], replica={"lag_s": 0.4})["replica"] == {"lag_s": 0.4}


def test_every_slack_result_says_it_too():
    assert executor._replica_hint(None) == ""
    assert "Ran on a read replica" in executor._replica_hint({"lag_s": 0.4})
    src = inspect.getsource(executor)
    for fn in ("_complete_no_result", "_complete_with_csv", "_complete_with_plan",
               "_complete_multi"):
        assert "_replica_hint(replica)" in inspect.getsource(getattr(executor, fn)), fn
    assert "_replica_hint(request)" not in src


# --- the inventory link ----------------------------------------------------------------

def _importer():
    sys.path.insert(0, str(SCRIPTS))
    import import_targets_from_inventory as mod
    return mod


def _servers():
    return [
        {"db_instance_identifier": "prod-ledger", "endpoint": "primary.example.test",
         "is_deleted": False, "is_read_replica": False, "replica_source": None},
        {"db_instance_identifier": "prod-ledger-read-1", "endpoint": "replica.example.test",
         "is_deleted": False, "is_read_replica": True, "replica_source": "prod-ledger"},
        {"db_instance_identifier": "prod-orders", "endpoint": "orders.example.test",
         "is_deleted": False, "is_read_replica": False, "replica_source": None},
    ]


def test_the_inventory_links_a_replica_to_its_primary():
    rows = [{"id": 7, "host": "primary.example.test", "replica_of": None, "engine": "postgres"},
            {"id": 99, "host": "replica.example.test", "replica_of": None, "engine": "postgres"},
            {"id": 8, "host": "orders.example.test", "replica_of": None, "engine": "postgres"}]
    assert _importer().plan_replica_links(_servers(), rows) == [(99, 7)]


def test_a_promoted_replica_is_unlinked_and_a_hand_link_it_does_not_know_is_kept():
    servers = [dict(s, is_read_replica=False, replica_source=None) for s in _servers()]
    rows = [{"id": 7, "host": "primary.example.test", "replica_of": None, "engine": "postgres"},
            {"id": 99, "host": "replica.example.test", "replica_of": 7, "engine": "postgres"},
            {"id": 50, "host": "elsewhere.example.test", "replica_of": 7, "engine": "postgres"}]
    assert _importer().plan_replica_links(servers, rows) == [(99, None)]


def test_a_replica_whose_primary_is_not_registered_stays_unlinked():
    rows = [{"id": 99, "host": "replica.example.test", "replica_of": None, "engine": "postgres"}]
    assert _importer().plan_replica_links(_servers(), rows) == []


def test_only_postgres_targets_are_linked():
    rows = [{"id": 7, "host": "primary.example.test", "replica_of": None, "engine": "postgres"},
            {"id": 99, "host": "replica.example.test", "replica_of": None, "engine": "clickhouse"}]
    assert _importer().plan_replica_links(_servers(), rows) == []


# --- the lockout -----------------------------------------------------------------------

def _lockout():
    sys.path.insert(0, str(SCRIPTS))
    import breakglass_lockout as mod
    return mod


def test_the_lockout_ends_replica_sessions_with_the_primarys_logins():
    src = inspect.getsource(_lockout().fleet)
    assert "LEFT JOIN target_servers p ON p.id = t.replica_of" in src
    assert "COALESCE(p.username_ddl, t.username_ddl)" in src
    assert "replica_of" in src and "sorted(" in src


def test_the_lockout_writes_no_role_change_on_a_replica(monkeypatch):
    mod = _lockout()
    ran = []

    class Cur:
        def execute(self, sql, params=None):
            ran.append(str(sql))
            self._last = str(sql)

        def fetchone(self):
            return (1,) if "pg_roles" in self._last or "count" in self._last else None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(mod, "_connect", lambda row, args: Conn())
    args = type("A", (), {"apply": True, "terminate": True, "admin_user": None})()
    row = {"alias": "prod-ledger-read-1", "replica_of": 7, "username": "reader",
           "username_rw": None, "username_ddl": None, "super_ddl_role": None}
    out = mod.lock_postgres(row, args)
    assert not any("ALTER ROLE" in s for s in ran)
    assert any("pg_terminate_backend" in s for s in ran)
    assert out["locked"] == ["reader"] and out["error"] is None
