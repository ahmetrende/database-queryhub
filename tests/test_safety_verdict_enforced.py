"""A blocked verdict has to actually stop something.

`query_safety` is the best-tested module in the project: 91% line coverage, a
corpus, three hypothesis properties. And yet both places that ENFORCE its
verdict were covered by nothing at all. Mutation-tested on this tree
2026-07-30, replacing each guard with `if False:`:

    core_submit.validate_submission   ->  1337 tests passed, exit 0
    executor._run                     ->  1337 tests passed, exit 0

Every existing test asserts on `report.blocked`. None asserts on the
consequence. That is exactly how a bypass lives behind a green suite: the
boundary moves, the tests still agree with it, and nobody notices the gate is
no longer wired to anything.

So these tests deliberately do NOT check what `analyze()` decides — that is
covered elsewhere, and duplicating it here would make them fail for the wrong
reason. They force a blocked verdict and assert on what the system DOES:
nothing is inserted, nothing is executed. Each one is written to fail when its
guard is removed; that property is asserted at the bottom of the file.
"""
import pytest

from dba_slack_bot import core_submit as cs
from dba_slack_bot import query_safety


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

@pytest.fixture
def submit_env(monkeypatch):
    """Everything validate_submission touches before the safety gate, stubbed
    so the gate is the only thing under test."""
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    # The safety gate now consults identity: `unrestricted` (the
    # super-admin path) is an INPUT to analyze(), so a submission cannot
    # be classified without knowing who is asking. These tests are about
    # an ordinary user, so answer no.
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(cs.requesters, "open_request_count", lambda uid: 0)
    monkeypatch.setattr(cs.cfg, "get_int",
                        lambda k, d=None: {"min_query_length": 1,
                                           "max_open_requests_per_user": 5}
                        .get(k, d if d is not None else 5))
    monkeypatch.setattr(cs.cfg, "get_setting", lambda k, d=None: d)
    target = type("T", (), {"id": 7, "engine": "postgres", "alias": "t",
                            "enabled": True, "host": "h", "port": 5432})()
    monkeypatch.setattr(cs.targets, "get", lambda tid: target)

    from dba_slack_bot import lifecycle
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)

    # Anything reached only AFTER the gate must blow up if it is reached, so a
    # removed gate cannot quietly succeed on a stubbed happy path.
    def _must_not_run(*a, **k):
        raise AssertionError(
            "reached past the safety gate with a blocked query")
    monkeypatch.setattr(cs.query_safety, "required_mode", _must_not_run)
    monkeypatch.setattr(cs.teams, "effective_grant_for_user", _must_not_run)
    return target


def _force_blocked(monkeypatch, message="nope"):
    """Make analyze() report blocked, whatever the SQL is. The point is to test
    the ENFORCEMENT, not to re-test the classifier."""
    report = query_safety.SafetyReport()
    report.blockers.append(message)
    monkeypatch.setattr(cs.query_safety, "analyze",
                        lambda sql, engine="postgres", unrestricted=False: report)
    return report


def test_a_blocked_query_is_rejected_at_submit(submit_env, monkeypatch):
    _force_blocked(monkeypatch, "blocked for a reason")
    out = cs.validate_submission(
        "U0EXAMPLE01", "someone", target_server_id=7, database_name="db",
        query="UPDATE t SET a = 1", justification=None)
    assert isinstance(out, cs.Rejection), f"got {out!r}"
    assert out.field == "query"
    assert "blocked for a reason" in out.message


def test_the_rejection_carries_the_blocker_text_to_the_user(submit_env,
                                                           monkeypatch):
    """A refusal the user cannot act on gets routed around, so the reason has
    to survive the trip out."""
    _force_blocked(monkeypatch, "Escape a quote as '' instead")
    out = cs.validate_submission(
        "U0EXAMPLE01", "someone", target_server_id=7, database_name="db",
        query="UPDATE t SET a = 1", justification=None)
    assert "Escape a quote as ''" in out.message


def test_nothing_is_prepared_for_a_blocked_query(submit_env, monkeypatch):
    """A Prepared is what create_request() inserts from. Returning one for a
    blocked query would put it in the queue for a DBA to approve."""
    _force_blocked(monkeypatch)
    out = cs.validate_submission(
        "U0EXAMPLE01", "someone", target_server_id=7, database_name="db",
        query="DROP TABLE t", justification=None)
    assert not isinstance(out, cs.Prepared)


def test_an_unblocked_query_does_get_past_the_gate(submit_env, monkeypatch):
    """The other half of the mutation argument: if this test passed no matter
    what, the ones above would prove nothing about the gate specifically."""
    clean = query_safety.SafetyReport()
    monkeypatch.setattr(cs.query_safety, "analyze",
                        lambda sql, engine="postgres", unrestricted=False: clean)
    with pytest.raises(AssertionError, match="reached past the safety gate"):
        cs.validate_submission(
            "U0EXAMPLE01", "someone", target_server_id=7, database_name="db",
            query="SELECT 1", justification=None)


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

def test_a_blocked_query_is_never_executed(monkeypatch):
    """The execution-time re-analysis exists because the SQL can change after
    approval (Request-changes / edit-and-resubmit). If its verdict is not
    enforced, an approved-then-edited query runs unchecked.

    The connection factory raises: reaching the database at all is the failure
    this test is looking for, and the request must be failed instead.
    """
    from dba_slack_bot import executor

    target = type("T", (), {"id": 7, "engine": "postgres", "alias": "t",
                            "host": "h", "port": 5432, "enabled": True})()
    monkeypatch.setattr(executor.targets, "get", lambda tid: target)
    monkeypatch.setattr(executor.engines, "is_executable", lambda e: True)
    monkeypatch.setattr(executor.admins, "is_super_admin", lambda uid: False)

    report = query_safety.SafetyReport()
    report.blockers.append("blocked at execution time")
    monkeypatch.setattr(executor.query_safety, "analyze",
                        lambda sql, engine="postgres", unrestricted=False: report)

    def _no_connections(*a, **k):
        raise AssertionError("opened a connection for a blocked query")
    monkeypatch.setattr(executor.psycopg, "connect", _no_connections)

    failed = {}

    def _fail(client, request, message, **kw):
        failed["message"] = message
    monkeypatch.setattr(executor, "_fail", _fail)

    request = {"id": 1, "query": "UPDATE t SET a = 1", "target_server_id": 7,
               "database_name": "db", "wants_result": True,
               "result_format": "csv", "requester_slack_id": "U0EXAMPLE01",
               "engine": "postgres", "required_tier": "rw"}

    executor._run(request, None)

    assert "blocked at execution time" in failed.get("message", ""), \
        "the request was not failed with the blocker text"


def test_the_execution_gate_reads_the_stored_query_not_the_submitted_one():
    """It re-analyzes `request["query"]`, which is the row as it stands now.
    Analyzing anything else would re-approve the text a DBA already saw and
    miss an edit made after approval."""
    import pathlib
    import re
    src = (pathlib.Path(__file__).resolve().parent.parent / "src"
           / "dba_slack_bot" / "executor.py").read_text(encoding="utf-8")
    # Matched on the first ARGUMENT rather than the whole call text: the call
    # gained `unrestricted=` (re-derived from the requester's current super-admin
    # standing) and wrapped onto three lines, which broke an exact-string
    # assertion while this property was untouched.
    m = re.search(r"query_safety\.analyze\(\s*(?P<first>[^,)]+)", src)
    assert m, "the execution-time safety gate is gone from executor.py"
    assert m.group("first").strip() == 'request["query"]', (
        f"the gate analyses {m.group('first').strip()} rather than the stored "
        f"query, so an edit made after approval would go unchecked")


# ---------------------------------------------------------------------------
# the guards these tests exist to protect
# ---------------------------------------------------------------------------

def test_both_enforcement_points_are_still_present_in_the_source():
    """Belt and braces for the specific mutation that survived: if either
    `if ...blocked:` is deleted or turned into a constant, this fails even
    should someone also weaken the behavioural tests above.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "dba_slack_bot"
    submit = (root / "core_submit.py").read_text(encoding="utf-8")
    execute = (root / "executor.py").read_text(encoding="utf-8")
    assert "if safety.blocked:" in submit
    assert "return Rejection(\"query\", \" \".join(safety.blockers)[:3000])" in submit
    assert "if report.blocked:" in execute
    assert '_fail(client, request, " ".join(report.blockers))' in execute
