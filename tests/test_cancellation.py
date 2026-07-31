"""Stopping a running query, and the runaway watchdog.

Written after a live incident on 2026-07-28. What was measured then, against a
production target, is what this module has to encode:

  * a backend blocked in `Client/ClientWrite` — the server pushing results at a
    client that has stopped reading — honours neither `statement_timeout` NOR
    `pg_cancel_backend`. A query with a 300s timeout was observed still running
    at 578s, and a cancel returned true while changing nothing.
  * `pg_terminate_backend` ended it at once.
  * the row stayed `executing` for 680s, because `reconcile_orphaned_executing`
    waits `execution_lease_sec` (900s) and is right to: a recent row may belong
    to a healthy sibling process.

So: a cancel that cannot escalate is a cancel that silently fails in the exact
case it is needed, and process liveness is not evidence of health. Both
properties are tested here.

The other property worth more than the feature: a pid is REUSED after a backend
exits, so signalling a pid recorded minutes ago could hit an unrelated session on
a production database. Every signal must be preceded by a check that the pid is
still running this request's query.
"""
import pytest

from queryhub import cancellation
from queryhub.cancellation import CancelOutcome


class FakeTargetCursor:
    """Stands in for a cursor on the TARGET. `alive` is consulted on every
    liveness probe, so a test can make the backend die after N probes — which is
    how "the cancel landed" and "the cancel did nothing" differ."""

    def __init__(self, alive_for_probes=99):
        self.alive_for_probes = alive_for_probes
        self.probes = 0
        self.signals = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "pg_stat_activity" in s:
            self.probes += 1
            self._alive = self.probes <= self.alive_for_probes
        elif "pg_cancel_backend" in s:
            self.signals.append(("cancel", params[0]))
            self._alive = True
        elif "pg_terminate_backend" in s:
            self.signals.append(("terminate", params[0]))
            self._alive = False
        else:
            self._alive = False

    def fetchone(self):
        return (1,) if getattr(self, "_alive", False) else None


@pytest.fixture
def wired(monkeypatch):
    """Wire cancellation.py to fakes: a request row, a target, and a cursor."""
    box = {
        "row": {"backend_pid": 4242, "target_server_id": 33,
                "database_name": "appdb", "st": "executing",
                "query": "select id, email from users"},
        "cursor": FakeTargetCursor(),
        "updates": [], "audits": [],
    }

    def fake_fetch_one(sql, params=None):
        if "backend_pid" in sql or "SELECT query FROM requests" in sql:
            return box["row"]
        return box["row"]

    monkeypatch.setattr(cancellation.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(cancellation.db, "fetch_all", lambda *a, **k: [])

    import contextlib

    class CtlCursor:
        def execute(self, sql, params=None):
            box["updates"].append((" ".join(sql.split()), params))
            self.rowcount = 1

        def fetchone(self):
            return {"id": 1}

    @contextlib.contextmanager
    def fake_txn():
        yield CtlCursor()

    monkeypatch.setattr(cancellation.db, "transaction", fake_txn)
    monkeypatch.setattr(cancellation.audit, "log_in",
                        lambda cur, rid, aid, an, action, details=None:
                        box["audits"].append(action))
    monkeypatch.setattr(cancellation.targets, "get",
                        lambda tid: type("T", (), {
                            "id": tid, "host": "db.example.test", "port": 5432,
                            "username": "reader"})())
    monkeypatch.setattr(cancellation.targets, "get_password", lambda tid: "pw")
    monkeypatch.setattr(cancellation.cfg, "target_ssl_kwargs", lambda: {})
    monkeypatch.setattr(cancellation.cfg, "get_int",
                        lambda k, d=None: 1 if k == "cancel_escalate_sec" else (d or 0))

    @contextlib.contextmanager
    def fake_connect(*a, **k):
        class Conn:
            def cursor(self):
                @contextlib.contextmanager
                def _c():
                    yield box["cursor"]
                return _c()
        yield Conn()

    import psycopg
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    return box


# ------------------------------------------------------------ the escalation


def test_a_cancel_that_works_does_not_terminate(wired):
    """The polite path: the backend dies on the cancel, the session survives."""
    wired["cursor"] = FakeTargetCursor(alive_for_probes=1)   # alive, then gone
    assert cancellation.stop_backend(1) == CancelOutcome.CANCELLED
    kinds = [k for k, _ in wired["cursor"].signals]
    assert kinds == ["cancel"], "terminated a backend that had already stopped"


def test_a_cancel_that_does_nothing_escalates_to_terminate(wired):
    """THE case from the incident. pg_cancel_backend returned true and the
    backend kept running, because a backend blocked in Client/ClientWrite never
    processes the interrupt. Without escalation the user's Cancel button would
    report success and change nothing."""
    wired["cursor"] = FakeTargetCursor(alive_for_probes=99)  # never dies
    assert cancellation.stop_backend(1) == CancelOutcome.TERMINATED
    kinds = [k for k, _ in wired["cursor"].signals]
    assert kinds == ["cancel", "terminate"], \
        "did not escalate — this is the failure that made the incident 10 min long"


def test_the_signals_target_the_recorded_pid(wired):
    wired["cursor"] = FakeTargetCursor(alive_for_probes=99)
    cancellation.stop_backend(1)
    assert {pid for _, pid in wired["cursor"].signals} == {4242}


# ------------------------------------- not signalling a stranger's backend


def test_a_pid_not_running_this_query_is_never_signalled(wired):
    """Pids are reused. Signalling one recorded minutes ago could hit an
    unrelated session on a production database — the worst outcome available
    here, and worse than failing to cancel."""
    wired["cursor"] = FakeTargetCursor(alive_for_probes=0)   # probe finds nothing
    assert cancellation.stop_backend(1) == CancelOutcome.NOT_RUNNING
    assert wired["cursor"].signals == [], "signalled a backend it could not identify"


def test_a_request_with_no_recorded_pid_fails_rather_than_guessing(wired):
    """Executions from before migration 084. Nothing to aim at, so say so."""
    wired["row"] = dict(wired["row"], backend_pid=None)
    assert cancellation.stop_backend(1) == CancelOutcome.FAILED
    assert wired["cursor"].signals == []


def test_a_finished_request_is_not_signalled(wired):
    wired["row"] = dict(wired["row"], st="completed")
    assert cancellation.stop_backend(1) == CancelOutcome.NOT_RUNNING
    assert wired["cursor"].signals == []


def test_an_unreachable_target_reports_failure_not_success(wired, monkeypatch):
    """Telling a user "cancelled" when nothing was cancelled is the one answer
    that must not happen."""
    import psycopg

    def boom(*a, **k):
        raise RuntimeError("could not connect")

    monkeypatch.setattr(psycopg, "connect", boom)
    assert cancellation.stop_backend(1) == CancelOutcome.FAILED


# ------------------------------------------------------------- the audit trail


def test_a_cancel_records_the_ask_and_the_outcome(wired):
    wired["cursor"] = FakeTargetCursor(alive_for_probes=1)
    cancellation.cancel(1, "U0DEV", "A Developer")
    assert "cancel_requested" in wired["audits"], "the ask was not audited"
    assert "cancelled_running" in wired["audits"], "the outcome was not audited"


def test_a_cancel_that_stopped_nothing_does_not_fail_the_row(wired):
    """If the query finished first, its result must survive — the user asked to
    stop it, not to discard a completed answer."""
    wired["cursor"] = FakeTargetCursor(alive_for_probes=0)
    cancellation.cancel(1, "U0DEV", "A Developer")
    failed = [s for s, _ in wired["updates"] if "status = 'failed'" in s]
    assert not failed, "failed a request that had already completed"


def test_failing_the_row_is_conditional_on_it_still_executing(wired):
    """Guards the race between the cancel and a normal finish: the UPDATE must
    carry `AND status = 'executing'` so a completed row is never overwritten."""
    wired["cursor"] = FakeTargetCursor(alive_for_probes=1)
    cancellation.cancel(1, "U0DEV", "A Developer")
    failed = [s for s, _ in wired["updates"] if "status = 'failed'" in s]
    assert failed, "no failure write at all"
    assert "status = 'executing'" in failed[0]


# ---------------------------------------------------------------- watchdog


def test_the_watchdog_is_wall_clock_not_liveness(monkeypatch):
    """Deliberately a different question from reconcile_orphaned_executing,
    which waits for the LEASE (900s) because a recent row may belong to a
    healthy sibling. A runaway can outlive that while holding a production
    connection, so this one asks only how long it has been running."""
    seen = {}

    def fake_fetch_all(sql, params=None):
        seen["sql"] = " ".join(sql.split())
        seen["params"] = params
        return []

    monkeypatch.setattr(cancellation.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(cancellation.cfg, "get_int",
                        lambda k, d=None: 420 if k == "execution_runaway_sec" else d)
    cancellation.sweep_runaways()
    assert "status = 'executing'" in seen["sql"]
    assert "executed_at <" in seen["sql"]
    assert seen["params"] == (420,)
    # It must NOT consult the lease — that is the other sweep's job.
    assert "lease" not in seen["sql"].lower()


def test_the_watchdog_can_be_switched_off(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(cancellation.db, "fetch_all",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [])
    monkeypatch.setattr(cancellation.cfg, "get_int", lambda k, d=None: 0)
    assert cancellation.sweep_runaways() == 0
    assert called["n"] == 0, "queried despite being disabled"


# ---------------------------------------------------------------------------
# The gap that turned a wedged query into an outage (2026-07-29).
#
# The liveness probe identified our backend by comparing pg_stat_activity.query
# against the head of the user's SQL. The wedged execution had moved on to a
# follow-up catalog query, so the text no longer matched, the probe reported
# `not_running`, and the watchdog marked the row failed while TERMINATING
# NOTHING — which is the one action that unblocks a client stuck on a backend
# blocked in ClientWrite. Identity now comes from application_name, which is a
# property of the session and survives whatever statement is running.
# ---------------------------------------------------------------------------


class _ProbeCur:
    """Answers the probe. `matched` is what the SQL would return."""

    def __init__(self, matched=True):
        self.matched = matched
        self.last = None

    def execute(self, sql, params=None):
        if "pg_stat_clear_snapshot" in sql:
            return
        self.last = (" ".join(sql.split()), params)

    def fetchone(self):
        return (1,) if self.matched else None


def test_the_probe_identifies_the_session_not_the_statement(monkeypatch):
    from queryhub import cancellation as c
    monkeypatch.setattr(c.db, "fetch_one",
                        lambda *a, **k: {"query": "SELECT * FROM big_table"})
    cur = _ProbeCur()
    assert c._matches_this_request(cur, 4242, 1987) is True
    sql, params = cur.last
    assert "application_name LIKE" in sql, \
        "identity must not depend on what the backend is running right now"
    assert params[0] == 4242
    assert params[1] == "%req=1987 %", \
        "the trailing space keeps req=198 from matching req=1987"


def test_the_query_text_survives_only_as_a_fallback(monkeypatch):
    """A session with no application_name — a pre-084 execution, or a connection
    something other than the executor opened."""
    from queryhub import cancellation as c
    monkeypatch.setattr(c.db, "fetch_one",
                        lambda *a, **k: {"query": "SELECT 1"})
    cur = _ProbeCur()
    c._matches_this_request(cur, 1, 2)
    sql, _ = cur.last
    assert "COALESCE(application_name, '') = ''" in sql
    assert "regexp_replace(query" in sql


def test_a_request_with_no_query_matches_nothing(monkeypatch):
    from queryhub import cancellation as c
    monkeypatch.setattr(c.db, "fetch_one", lambda *a, **k: None)
    assert c._matches_this_request(_ProbeCur(), 1, 2) is False


# ---------------------------------------------------------------------------
# Withdrawing: the same button, before there is a backend to signal.
# ---------------------------------------------------------------------------


class _TxnCur:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))


def _fake_txn(monkeypatch, cur):
    import contextlib
    from queryhub import cancellation as c

    @contextlib.contextmanager
    def txn():
        yield cur
    monkeypatch.setattr(c.db, "transaction", txn)
    monkeypatch.setattr(c.audit, "log_in", lambda *a, **k: None)


def test_withdrawing_closes_the_row_and_says_who(monkeypatch):
    from queryhub import cancellation as c
    cur = _TxnCur(rowcount=1)
    _fake_txn(monkeypatch, cur)
    assert c.withdraw(1990, "U1", "Ada") is True
    sql, params = cur.sql[0]
    assert "status = 'cancelled'" in sql
    assert "Ada" in params[0]


def test_withdrawing_is_conditional_on_the_status_it_was_read_at(monkeypatch):
    """The race that matters: the executor claiming the row between the read and
    the UPDATE. Losing it must be a no-op, not a request marked cancelled while a
    query runs against production."""
    from queryhub import cancellation as c
    cur = _TxnCur(rowcount=0)
    _fake_txn(monkeypatch, cur)
    assert c.withdraw(1990, "U1", "Ada") is False
    sql, params = cur.sql[0]
    assert "status = ANY(%s)" in sql
    assert "executing" not in params[2], \
        "an executing request must not be withdrawable — it needs a real cancel"


def test_the_withdrawable_states_are_the_pre_execution_ones():
    from queryhub import cancellation as c
    assert set(c.WITHDRAWABLE) == {"pending", "changes_requested",
                                   "approved", "scheduled"}
