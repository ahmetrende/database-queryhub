"""Where a request runs when a super-admin chose it: execution.

`requests.run_on` is intent, and the executor decides again when it runs:

- a requester who is no longer a super-admin runs AUTO -- the choice is
  dropped, and the drop is recorded on the execution_started row;
- `primary` never consults a replica, for any tier, on any engine;
- a chosen replica skips every automatic rule (the routing switch,
  node-local reads, read-your-writes, the lag limit) but must still answer and
  be in recovery; measured now, with the primary allowed to be down;
- a chosen replica that cannot run the query, before or during the run, FAILS
  the request with its name and the reason -- it never falls back to the
  primary;
- the honoured choice writes `execution_run_on_forced` in the claim's
  transaction, and no server-side "ran on" line: the web builds that sentence
  from `ranOn`, while the automatic replica line stays as it was;
- a scheduled request keeps its choice, re-derived when it runs;
- masking is decided by the REQUEST's target, the primary, wherever the query
  executes (the operator's rule: any masking on a primary holds on its
  replicas).

Automatic routing itself is pinned by test_read_replicas.py; the tests here
that touch it only prove it is unchanged for a request nobody forced.
"""
from __future__ import annotations

import contextlib
import itertools

import psycopg
import pytest

from queryhub import executor, pii, query_safety, replicas
from queryhub.web import routes_queries

PRIMARY_HOST = "primary.example.test"
REPLICA_HOST = "replica.example.test"
PRIMARY = type("T", (), {"id": 7, "alias": "prod-ledger", "engine": "postgres",
                         "host": PRIMARY_HOST, "port": 5432,
                         "default_database": "ledger", "enabled": True,
                         "username": "reader", "replica_of": None})()
REPLICA_ROW = {"id": 99, "alias": "prod-ledger-read-1", "host": REPLICA_HOST,
               "port": 5432}
CAUGHT_UP = {PRIMARY_HOST: ("3B4F/FA6EE000",), REPLICA_HOST: (True, 0, 0.0)}


@pytest.fixture(autouse=True)
def _fresh_health_cache():
    replicas._health.clear()
    yield
    replicas._health.clear()


class _Result:
    csv_path = None
    notices: list = []


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    """executor._run down the Postgres path with its collaborators faked, and
    the REAL routing decision in front of it: replicas.choose / replicas.chosen
    run for real over a fake psycopg.connect, which answers the health probe
    from box["probe"] and records every execution connection's host.

    box["super"]       is_super_admin, live, at execution
    box["run_on"]      the stored choice on the request row
    box["probe"]       {host: answer tuple or Exception} for the health check
    box["replica_error"]  raised by the statement when it runs on the replica
    box["real_statement"] run the real _execute_main_statement over box["rows"]
    """
    box = {"hosts": [], "probes": [], "claims": [], "audits": [], "updates": [],
           "finalized": [], "failed": [], "unhealthy": [], "exemption_targets": [],
           "super": True, "run_on": None, "probe": dict(CAUGHT_UP),
           "replica_error": None, "real_statement": False, "wrote_recently": False,
           "settings": {"replica_routing": "on"}, "txn": itertools.count(1)}
    report = query_safety.SafetyReport(main_tier="ro", statements=[
        query_safety.StatementInfo(raw="SELECT id, email FROM customers",
                                   rewritten="SELECT id, email FROM customers",
                                   kind="ro", leading="SELECT")])
    box["report"] = report
    monkeypatch.setattr(executor.query_secrets, "statement_to_run", lambda r: r["query"])
    monkeypatch.setattr(executor.targets, "get", lambda tid: PRIMARY)
    monkeypatch.setattr(executor.engines, "is_executable", lambda e: True)
    monkeypatch.setattr(executor.admins, "is_super_admin", lambda uid: box["super"])
    monkeypatch.setattr(executor.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(executor.query_safety, "analyze", lambda *a, **k: box["report"])
    monkeypatch.setattr(executor.teams, "effective_mode_for_database", lambda *a: "ddl")
    monkeypatch.setattr(executor.targets, "get_credentials",
                        lambda tid, mode: ("reader", "pw"))
    monkeypatch.setattr(replicas, "replicas_of",
                        lambda pid: [REPLICA_ROW] if pid == 7 else [])
    monkeypatch.setattr(replicas, "_wrote_recently",
                        lambda *a: box["wrote_recently"])
    monkeypatch.setattr(replicas, "mark_unhealthy",
                        lambda rid, why: box["unhealthy"].append((rid, why)))
    monkeypatch.setattr(replicas.log, "info", lambda *a, **k: None)
    monkeypatch.setattr(executor.cfg, "get_int", lambda k, d=None: d)
    monkeypatch.setattr(executor.cfg, "get_setting",
                        lambda k, d=None: box["settings"].get(k, d))
    monkeypatch.setattr(executor.cfg, "target_ssl_kwargs", lambda host=None: {})
    monkeypatch.setattr(executor.row_limits, "effective_caps", lambda uid: (1000, 10 ** 6))
    monkeypatch.setattr(executor, "_super_role_for", lambda *a: None)
    monkeypatch.setattr(executor, "_team_role_for", lambda *a: None)
    monkeypatch.setattr(executor, "_cancel_requested", lambda rid: False)
    monkeypatch.setattr(executor, "_build_application_name", lambda r: "queryhub-test")
    monkeypatch.setattr(executor.pg_types, "register_infinity_safe_loaders", lambda c: None)
    monkeypatch.setattr(executor, "CSV_DIR", tmp_path)

    real_execute_main = executor._execute_main_statement

    def execute_main(cur, *a, **k):
        if cur.on_replica and box["replica_error"] is not None:
            raise box["replica_error"]
        if box["real_statement"]:
            return real_execute_main(cur, *a, **k)
        return _Result()
    monkeypatch.setattr(executor, "_execute_main_statement", execute_main)
    monkeypatch.setattr(executor, "_finalize",
                        lambda *a, **k: box["finalized"].append(
                            (k.get("replica"), k.get("target").id)))
    monkeypatch.setattr(executor, "_fail",
                        lambda client, request, msg, **k: box["failed"].append(msg))

    def log_in(cur, rid, actor, name, action, details=None):
        box["audits"].append((action, details, cur.txn, actor))
    monkeypatch.setattr(executor.audit, "log_in", log_in)
    monkeypatch.setattr(executor.audit, "log", lambda *a, **k: None)

    class TxnCur:
        rowcount = 1

        def __init__(self):
            self.txn = next(box["txn"])

        def execute(self, sql, params=None):
            flat = " ".join(sql.split())
            (box["claims"] if "status = 'executing'" in flat else box["updates"]).append(
                (flat, params, self.txn))

        def fetchall(self):
            # What the scheduler's UPDATE ... RETURNING hands back.
            return [dict(r) for r in box.get("due", [])]

    @contextlib.contextmanager
    def txn():
        yield TxnCur()
    monkeypatch.setattr(executor.db, "transaction", txn)

    def fetch_one(sql, params=None):
        if "FROM target_servers" in sql:
            return {"alias": REPLICA_ROW["alias"]}
        return {"status": "executing"}
    monkeypatch.setattr(executor.db, "fetch_one", fetch_one)

    @contextlib.contextmanager
    def connect(host=None, port=None, **kw):
        if kw.get("application_name") == "queryhub:replica-health":
            box["probes"].append(host)
            answer = box["probe"].get(host)
            if isinstance(answer, Exception):
                raise answer

            class ProbeCur:
                def execute(self, sql, params=None):
                    pass

                def fetchone(self):
                    return answer

            class ProbeConn:
                def cursor(self):
                    @contextlib.contextmanager
                    def c():
                        yield ProbeCur()
                    return c()
            yield ProbeConn()
            return

        box["hosts"].append(host)
        on_replica = host == REPLICA_HOST

        class Cur:
            description = [("id",), ("email",)]
            rowcount = -1

            def __init__(self):
                self.on_replica = on_replica

            def execute(self, sql, params=None, prepare=False):
                pass

            def fetchone(self):
                return (4242,)

            def stream(self, sql):
                return iter([(1, "someone@example.test")])

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
    monkeypatch.setattr(psycopg, "connect", connect)
    return box


def _run(box, query="SELECT id, email FROM customers"):
    executor._run({"id": 1, "query": query, "target_server_id": 7,
                   "database_name": "ledger", "wants_result": True,
                   "result_format": "csv", "requester_slack_id": "U0EXAMPLE001",
                   "requester_name": "Ex", "engine": "postgres",
                   "required_tier": "ro", "run_on": box["run_on"]}, None)


def _audit(box, action):
    return next((d for a, d, _t, _u in box["audits"] if a == action), None)


# --- authority is re-derived --------------------------------------------------------

def test_a_demoted_super_admins_choice_is_dropped_and_runs_auto(run_env, monkeypatch):
    run_env.update(super=False, run_on="replica:99")
    monkeypatch.setattr(replicas, "chosen",
                        lambda *a: pytest.fail("honoured a choice the requester lost"))
    _run(run_env)
    # Auto, and auto finds the healthy replica: it is the automatic rules that
    # sent it there, so nothing says "chosen".
    assert run_env["hosts"] == [REPLICA_HOST]
    assert _audit(run_env, "execution_run_on_forced") is None
    assert _audit(run_env, "execution_started")["run_on_ignored"] == "replica:99"
    assert run_env["finalized"] == [({"lag_s": 0.0}, 7)]
    assert run_env["failed"] == []


def test_a_demoted_super_admins_primary_choice_runs_auto_too(run_env):
    run_env.update(super=False, run_on="primary")
    _run(run_env)
    assert run_env["hosts"] == [REPLICA_HOST]          # auto routed it
    assert _audit(run_env, "execution_run_on_forced") is None
    assert _audit(run_env, "execution_started")["run_on_ignored"] == "primary"


# --- primary -------------------------------------------------------------------------

def test_primary_never_consults_a_replica(run_env, monkeypatch):
    run_env["run_on"] = "primary"
    monkeypatch.setattr(replicas, "choose", lambda *a: pytest.fail("routed a forced primary"))
    monkeypatch.setattr(replicas, "replicas_of", lambda pid: pytest.fail("looked for replicas"))
    _run(run_env)
    assert run_env["hosts"] == [PRIMARY_HOST] and run_env["probes"] == []
    claim_sql, claim_params, claim_txn = run_env["claims"][0]
    assert claim_params[1] is None                     # executed_target_id: its own
    assert _audit(run_env, "execution_run_on_forced") == {
        "requested": "primary", "ran_on": "primary", "target_id": 7,
        "target": "prod-ledger", "lag_s": None}
    # No server line for a chosen node: the client builds it from ranOn.
    assert run_env["finalized"] == [(None, 7)]


def test_the_choice_is_audited_in_the_claims_own_transaction(run_env):
    """Written with the claim, before the query runs: a run that then fails is
    still on the record, and a claim that is lost writes nothing."""
    run_env["run_on"] = "primary"
    _run(run_env)
    claim_txn = run_env["claims"][0][2]
    forced = next(t for a, _d, t, _u in run_env["audits"] if a == "execution_run_on_forced")
    started = next(t for a, _d, t, _u in run_env["audits"] if a == "execution_started")
    assert forced == claim_txn == started
    actor = next(u for a, _d, _t, u in run_env["audits"] if a == "execution_run_on_forced")
    assert actor == "U0EXAMPLE001"


def test_primary_holds_for_a_write_too(run_env):
    run_env["run_on"] = "primary"
    run_env["report"] = query_safety.SafetyReport(main_tier="rw", statements=[
        query_safety.StatementInfo(raw="UPDATE t SET x = 1 WHERE id = 1",
                                   rewritten="UPDATE t SET x = 1 WHERE id = 1",
                                   kind="rw", leading="UPDATE")])
    _run(run_env)
    assert run_env["hosts"] == [PRIMARY_HOST] and run_env["failed"] == []


# --- a chosen replica skips the automatic rules ---------------------------------------

def test_a_chosen_replica_reads_its_own_pg_stat_activity(run_env):
    """The node-local rule keeps these on the primary for auto. Chosen, it is
    exactly the read the person wanted: the REPLICA's sessions."""
    run_env["run_on"] = "replica:99"
    _run(run_env, query="SELECT pid, state FROM pg_stat_activity")
    assert run_env["hosts"] == [REPLICA_HOST]


def test_a_chosen_replica_ignores_read_your_writes(run_env, monkeypatch):
    run_env["run_on"] = "replica:99"
    monkeypatch.setattr(replicas, "_wrote_recently",
                        lambda *a: pytest.fail("checked recent writes for a chosen replica"))
    _run(run_env)
    assert run_env["hosts"] == [REPLICA_HOST]


def test_a_chosen_replica_past_the_lag_limit_still_runs_and_says_how_far(run_env):
    run_env["run_on"] = "replica:99"
    run_env["probe"][REPLICA_HOST] = (True, 10 ** 9, 45.0)     # limit is 10 s
    _run(run_env)
    assert run_env["hosts"] == [REPLICA_HOST]
    assert _audit(run_env, "execution_run_on_forced") == {
        "requested": "replica:99", "ran_on": "replica", "target_id": 99,
        "target": "prod-ledger-read-1", "lag_s": 45.0}
    started = _audit(run_env, "execution_started")
    assert started["replica"] == "prod-ledger-read-1" and started["replica_lag_s"] == 45.0
    claim_params = run_env["claims"][0][1]
    assert claim_params[1] == 99                       # executed_target_id
    # No server line, not even the automatic one: this was not auto.
    assert run_env["finalized"] == [(None, 7)]


def test_a_chosen_replica_does_not_need_the_routing_switch(run_env):
    """`replica_routing` governs automatic routing only."""
    run_env["run_on"] = "replica:99"
    run_env["settings"]["replica_routing"] = "off"
    _run(run_env)
    assert run_env["hosts"] == [REPLICA_HOST]


def test_a_chosen_replica_runs_while_its_primary_is_down(run_env):
    """Reading a replica because the primary is down is a reason to choose
    one. The lag then comes from the replica's own replay age."""
    run_env["run_on"] = "replica:99"
    run_env["probe"][PRIMARY_HOST] = psycopg.OperationalError("connection refused")
    run_env["probe"][REPLICA_HOST] = (True, None, 3.2)
    _run(run_env)
    assert run_env["hosts"] == [REPLICA_HOST]
    assert _audit(run_env, "execution_run_on_forced")["lag_s"] == 3.2


def test_a_chosen_replica_whose_lag_cannot_be_measured_still_runs(run_env):
    run_env["run_on"] = "replica:99"
    run_env["probe"][REPLICA_HOST] = (True, 8192, None)
    _run(run_env)
    assert run_env["hosts"] == [REPLICA_HOST]
    assert _audit(run_env, "execution_run_on_forced")["lag_s"] is None


def test_a_chosen_replica_is_measured_now_not_from_the_cache(run_env):
    """A check another request's failure left behind does not decide this
    one: the probe runs even with a fresh 'unhealthy' entry cached."""
    run_env["run_on"] = "replica:99"
    replicas._health[99] = (float("inf"), replicas.Health(False, None, "failed a query"))
    _run(run_env)
    assert REPLICA_HOST in run_env["probes"] and run_env["hosts"] == [REPLICA_HOST]


# --- a chosen replica that cannot run it: fail, never the primary -----------------------

@pytest.mark.parametrize("answer,why", [
    (psycopg.OperationalError("timeout expired"), "is unreachable (OperationalError)"),
    ((False, None, None), "is not in recovery"),
])
def test_an_unusable_chosen_replica_fails_the_request_by_name(run_env, answer, why):
    run_env["run_on"] = "replica:99"
    run_env["probe"][REPLICA_HOST] = answer
    _run(run_env)
    assert run_env["hosts"] == [], "the query ran somewhere"
    assert run_env["claims"] == [], "the request was claimed for a run that cannot happen"
    (msg,) = run_env["failed"]
    assert "`prod-ledger-read-1`" in msg and why in msg
    assert "It was not run on the primary instead." in msg
    assert _audit(run_env, "execution_run_on_forced") is None


def test_a_replica_switched_off_after_submit_fails_the_request(run_env, monkeypatch):
    run_env["run_on"] = "replica:99"
    monkeypatch.setattr(replicas, "replicas_of", lambda pid: [])
    _run(run_env)
    assert run_env["hosts"] == [] and run_env["probes"] == []
    (msg,) = run_env["failed"]
    assert "`prod-ledger-read-1` is no longer an enabled read replica of `prod-ledger`" in msg


def test_a_chosen_replica_cannot_run_a_write(run_env):
    run_env["run_on"] = "replica:99"
    run_env["report"] = query_safety.SafetyReport(main_tier="rw", statements=[
        query_safety.StatementInfo(raw="UPDATE t SET x = 1 WHERE id = 1",
                                   rewritten="UPDATE t SET x = 1 WHERE id = 1",
                                   kind="rw", leading="UPDATE")])
    _run(run_env)
    assert run_env["hosts"] == []
    assert "read-only statements only" in run_env["failed"][0]


def test_a_chosen_replica_that_fails_mid_run_is_not_rerun_on_the_primary(run_env):
    run_env["run_on"] = "replica:99"
    run_env["replica_error"] = psycopg.errors.SerializationFailure(
        "canceling statement due to conflict with recovery")
    _run(run_env)
    assert run_env["hosts"] == [REPLICA_HOST], "it was run again somewhere else"
    assert _audit(run_env, "replica_fallback") is None
    assert not any("executed_target_id = NULL" in sql for sql, _p, _t in run_env["updates"])
    (msg,) = run_env["failed"]
    assert "`prod-ledger-read-1`" in msg and "conflict with recovery" in msg
    assert "It was not run on the primary instead." in msg
    assert run_env["unhealthy"] == [(99, "failed a query (SerializationFailure)")]
    # It did run there -- and failed there -- so the choice stays on record.
    assert _audit(run_env, "execution_run_on_forced")["ran_on"] == "replica"


def test_a_timeout_on_a_chosen_replica_is_reported_as_a_timeout(run_env):
    run_env["run_on"] = "replica:99"
    run_env["replica_error"] = psycopg.errors.QueryCanceled(
        "canceling statement due to statement timeout")
    _run(run_env)
    assert run_env["hosts"] == [REPLICA_HOST]
    assert "timeout" in run_env["failed"][0]


def test_a_choice_that_cannot_be_read_is_refused_not_run_as_auto(run_env, monkeypatch):
    run_env["run_on"] = "replica:garbled"
    monkeypatch.setattr(replicas, "choose", lambda *a: pytest.fail("ran it as auto"))
    _run(run_env)
    assert run_env["hosts"] == [] and run_env["failed"]


def test_chosen_never_raises(monkeypatch):
    monkeypatch.setattr(replicas, "replicas_of",
                        lambda pid: (_ for _ in ()).throw(RuntimeError("bot DB blinked")))
    monkeypatch.setattr(replicas.log, "exception", lambda *a, **k: None)
    d = replicas.chosen(PRIMARY, 99, "ro", "u", "p")
    assert d.route is None and "replica #99 could not be checked (RuntimeError)" == d.refused


def test_only_postgres_has_replicas_to_choose():
    ch = type("T", (), {"id": 8, "alias": "events", "engine": "clickhouse"})()
    d = replicas.chosen(ch, 99, "ro", "u", "p")
    assert d.route is None and "not a PostgreSQL connection" in d.refused


# --- a scheduled request keeps its choice --------------------------------------------------

def _due_row(run_on):
    """A scheduled row exactly as the scheduler's RETURNING yields it: every
    column of the shared list, and nothing else."""
    from queryhub import core_submit
    cols = [c.strip() for c in core_submit.REQUEST_RETURNING.split(",")]
    row = {c: None for c in cols}
    row.update(id=1, requester_slack_id="U0EXAMPLE001", requester_name="Ex",
               target_server_id=7, database_name="ledger",
               query="SELECT pid, state FROM pg_stat_activity", wants_result=True,
               result_format="csv", status="approved", origin="web",
               engine="postgres", required_tier="ro", unmasked=False, run_on=run_on)
    return row


@pytest.mark.parametrize("still_super", [True, False])
def test_a_scheduled_forced_run_keeps_its_choice_and_rederives_it(run_env, monkeypatch,
                                                                  still_super):
    """Submitted with a schedule and runOn=replica; dispatched later by the
    scheduler. The choice travels on the row the scheduler hands over, and the
    standing behind it is decided at run time, not at submit."""
    run_env["due"] = [_due_row("replica:99")]
    handed = []
    monkeypatch.setattr(executor, "submit", lambda row, client: handed.append(row))
    monkeypatch.setattr(executor.notifications, "update_user_scheduled_dm",
                        lambda *a, **k: None)
    assert executor.dispatch_due(None) == 1
    dispatch_sql = next(sql for sql, _p, _t in run_env["updates"]
                        if sql.startswith("UPDATE requests SET status = 'approved'"))
    assert "run_on" in dispatch_sql.split("RETURNING", 1)[1]
    (row,) = handed
    assert row["run_on"] == "replica:99"

    run_env["super"] = still_super
    executor._run(row, None)
    if still_super:
        # Honoured: on the replica, although pg_stat_activity would keep an
        # automatic read on the primary.
        assert run_env["hosts"] == [REPLICA_HOST]
        assert _audit(run_env, "execution_run_on_forced")["requested"] == "replica:99"
    else:
        # The standing is gone by run time: auto decides, and auto keeps a
        # node-local read on the primary.
        assert run_env["hosts"] == [PRIMARY_HOST]
        assert _audit(run_env, "execution_run_on_forced") is None
        assert _audit(run_env, "execution_started")["run_on_ignored"] == "replica:99"
    assert run_env["failed"] == []


# --- auto is unchanged -----------------------------------------------------------------

def test_a_super_admin_who_chose_nothing_gets_todays_routing(run_env):
    run_env["run_on"] = None
    _run(run_env, query="SELECT pid FROM pg_stat_activity")
    assert run_env["hosts"] == [PRIMARY_HOST]          # node-local: stays put
    assert _audit(run_env, "execution_started")["replica_skipped"].startswith(
        "reads pg_stat_activity")
    assert _audit(run_env, "execution_run_on_forced") is None


def test_auto_still_falls_back_to_the_primary(run_env):
    run_env["replica_error"] = psycopg.errors.SerializationFailure(
        "canceling statement due to conflict with recovery")
    _run(run_env)
    assert run_env["hosts"] == [REPLICA_HOST, PRIMARY_HOST]
    assert _audit(run_env, "replica_fallback")["replica"] == "prod-ledger-read-1"
    assert run_env["finalized"] == [(None, 7)]
    assert run_env["failed"] == []


def test_auto_still_respects_the_lag_limit(run_env):
    run_env["probe"][REPLICA_HOST] = (True, 10 ** 9, 45.0)
    _run(run_env)
    assert run_env["hosts"] == [PRIMARY_HOST]
    assert "45s behind (limit 10s)" in _audit(run_env, "execution_started")["replica_skipped"]


def test_auto_calls_the_primary_being_down_what_it_is():
    """The automatic check still refuses without the primary's position; the
    reason now says it was the primary that did not answer."""
    import contextlib as _c

    @_c.contextmanager
    def connect(host=None, **kw):
        raise psycopg.OperationalError("refused")
        yield  # pragma: no cover
    orig = psycopg.connect
    psycopg.connect = connect
    try:
        h = replicas._probe(PRIMARY, REPLICA_ROW, "u", "p")
    finally:
        psycopg.connect = orig
    assert not h.ok and h.reason == "primary unreachable (OperationalError)"


# --- SQL Server: primary skips the readable secondary ------------------------------------

@pytest.mark.parametrize("force,resolved", [(True, False), (False, True)])
def test_sql_server_primary_skips_the_readable_secondary(monkeypatch, force, resolved):
    from queryhub import mssql_exec
    seen = {"resolved": False, "connect": []}

    def resolve(*a, **k):
        seen["resolved"] = True
        return ("secondary.example.test", 1433)

    class Conn:
        def cursor(self):
            return object()

        def commit(self):
            pass

        def close(self):
            pass
    monkeypatch.setattr(mssql_exec, "resolve_ro_endpoint", resolve)
    monkeypatch.setattr(mssql_exec, "connect",
                        lambda host, port, *a, **k: seen["connect"].append(
                            (host, k.get("read_only"))) or Conn())
    monkeypatch.setattr(mssql_exec, "describe_columns", lambda *a: None)
    monkeypatch.setattr(executor, "_execute_main_statement", lambda *a, **k: _Result())
    monkeypatch.setattr(executor, "_finalize", lambda *a, **k: None)
    target = type("T", (), {"id": 5, "alias": "legacy-sql", "engine": "mssql",
                            "host": "listener.example.test", "port": 1433})()
    report = query_safety.SafetyReport(main_tier="ro", statements=[
        query_safety.StatementInfo(raw="SELECT 1", rewritten="SELECT 1", kind="ro",
                                   leading="SELECT")])
    executor._run_mssql(None, {"id": 3, "database_name": "app", "wants_result": True,
                               "requester_slack_id": "U0EXAMPLE001"},
                        target, "ro", report, "u", "p", 30, 100, 10 ** 6,
                        force_primary=force)
    assert seen["resolved"] is resolved
    if force:
        assert seen["connect"] == [("listener.example.test", False)]
    else:
        assert seen["connect"] == [("secondary.example.test", True)]


# --- masking: the primary's rules, wherever it runs --------------------------------------

@pytest.mark.parametrize("run_on", [None, "replica:99"])
def test_masking_is_decided_by_the_requests_target_not_the_replica(run_env, monkeypatch,
                                                                   run_on):
    """The operator's rule: any masking on a primary also holds on its replicas.
    Exemptions are looked up by the REQUEST's target (7), never by the id of
    the replica that executed the query (99) -- for an automatic replica run
    and for a chosen one. The real statement path runs here, over a streamed
    result, so this is the lookup the executor actually makes."""
    run_env.update(run_on=run_on, real_statement=True)
    seen = []
    monkeypatch.setattr(pii, "is_enabled", lambda: True)
    monkeypatch.setattr(executor.pii_lineage, "source_columns", lambda *a, **k: None)
    monkeypatch.setattr(pii, "column_pii_map", lambda *a, **k: {})
    monkeypatch.setattr(pii, "exemption_decision",
                        lambda tid, *a, **k: seen.append(("decision", tid)) or (False, set()))
    monkeypatch.setattr(pii, "exemption_namescan",
                        lambda tid, *a, **k: seen.append(("namescan", tid)) or set())
    _run(run_env)
    assert run_env["hosts"] == [REPLICA_HOST], "the query did not run on the replica"
    assert run_env["claims"][0][1][1] == 99
    assert seen == [("decision", 7), ("namescan", 7)]
    assert [t for _note, t in run_env["finalized"]] == [7]   # the exemption audit's target


def test_the_result_grid_marks_masked_columns_by_the_requests_target(monkeypatch):
    """The header dots read the same rules, again by target_server_id."""
    seen = []
    monkeypatch.setattr(pii, "is_enabled", lambda: True)
    monkeypatch.setattr(pii, "exemption_decision",
                        lambda tid, *a, **k: seen.append(tid) or (False, set()))
    monkeypatch.setattr(pii, "exemption_namescan",
                        lambda tid, *a, **k: seen.append(tid) or set())
    monkeypatch.setattr(pii, "column_pii_map", lambda cols, sql=None, **k: {1: "email"})
    row = {"id": 1, "target_server_id": 7, "executed_target_id": 99,
           "database_name": "ledger", "query": "SELECT id, email FROM customers",
           "requester_slack_id": "U0EXAMPLE001"}
    assert routes_queries._masked_pii_cols(row, ["id", "email"]) == ["email"]
    assert seen == [7, 7]
