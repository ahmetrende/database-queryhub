"""Masking comes off only for a super-admin, only when asked, and never silently.

Three separate facts have to line up before a result ships unmasked:

  1. the requester ASKED (`requests.unmasked`, recorded at submit),
  2. they are a super-admin AT EXECUTION TIME, re-checked then, not at submit,
  3. and the run writes an audit row saying masking was skipped.

The order matters. (1) alone is a wish; (2) alone is not a request; and without
(3) the "everything is logged" claim has a hole exactly where the sensitive data
is. Each of the three is tested on its own, and the pairs that must NOT be enough
are tested too.
"""
from __future__ import annotations

import pytest

from dba_slack_bot import core_submit as cs
from dba_slack_bot import executor as ex
from dba_slack_bot import lifecycle, targets

SQL = "SELECT id, email FROM customers WHERE id = 5"


# ---------------------------------------------------------------------------
# submit: who may even ask
# ---------------------------------------------------------------------------

@pytest.fixture
def submit(monkeypatch):
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(cs.cfg, "get_int", lambda k, d=None: d if d is not None else 5)
    monkeypatch.setattr(cs.cfg, "get_setting", lambda k, d=None: d)
    monkeypatch.setattr(cs.cfg, "get_bool", lambda k, d=False: d)
    target = targets.TargetServer(
        id=7, alias="t", host="h", port=5432, default_database="db",
        username="u", enabled=True, notes=None, engine="postgres")
    monkeypatch.setattr(cs.targets, "get", lambda tid: target)
    reached = {}

    def _stop(*a, **k):
        reached["past_the_gate"] = True
        raise AssertionError("past the gate")

    monkeypatch.setattr(cs.query_safety, "required_mode", _stop)

    def go(*, super_admin: bool, unmasked: bool):
        monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: super_admin)
        try:
            return cs.validate_submission(
                "U0EXAMPLE01", "someone", target_server_id=7, database_name="db",
                query=SQL, justification="because", unmasked=unmasked)
        except AssertionError as e:
            if "past the gate" not in str(e):
                raise
            return "PASSED_THE_GATE"

    return go


def test_an_ordinary_user_cannot_ask_for_an_unmasked_result(submit):
    out = submit(super_admin=False, unmasked=True)
    assert isinstance(out, cs.Rejection)
    assert out.field == "unmasked"
    assert out.reason == "not_super_admin"


def test_the_refusal_is_loud_rather_than_a_silent_downgrade(submit):
    """Masking them anyway would be just as safe for the data and would hide a
    client sending a flag it has no business sending."""
    out = submit(super_admin=False, unmasked=True)
    assert out != "PASSED_THE_GATE", (
        "the request went through with the flag quietly dropped")


def test_an_ordinary_user_is_unaffected_when_they_do_not_ask(submit):
    assert submit(super_admin=False, unmasked=False) == "PASSED_THE_GATE"


def test_a_super_admin_may_ask(submit):
    assert submit(super_admin=True, unmasked=True) == "PASSED_THE_GATE"


def test_the_intent_lands_on_the_prepared_submission(monkeypatch):
    """`Prepared.unmasked` is what reaches the INSERT, so it has to carry."""
    assert "unmasked" in {f.name for f in __import__("dataclasses").fields(cs.Prepared)}


# ---------------------------------------------------------------------------
# execution: whether the ask is honoured, decided NOW
# ---------------------------------------------------------------------------

def _target():
    return type("T", (), {"enabled": True, "engine": "postgres", "alias": "svc",
                          "id": 7, "default_database": "app"})()


@pytest.fixture
def run(monkeypatch):
    seen: dict = {}
    state = {"super": False}
    monkeypatch.setattr(ex.targets, "get", lambda tid: _target())
    monkeypatch.setattr(ex.engines, "is_executable", lambda e: True)
    monkeypatch.setattr(ex.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(ex.admins, "is_super_admin", lambda uid: state["super"])
    monkeypatch.setattr(ex.requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(ex.teams, "effective_mode_for_database", lambda *a, **k: "ddl")
    monkeypatch.setattr(ex, "_fail", lambda c, r, m: seen.setdefault("fail", m))
    monkeypatch.setattr(ex.audit, "log",
                        lambda *a, **k: seen.setdefault("audit", (a, k)))

    def _creds(*a, **k):
        seen["unmask_passed_down"] = None      # filled by the stub below
        raise LookupError("stop")

    monkeypatch.setattr(ex.targets, "get_credentials", _creds)

    def go(*, asked: bool, super_admin: bool):
        state["super"] = super_admin
        seen.clear()
        request = {
            "id": 1, "target_server_id": 7, "database_name": "app", "query": SQL,
            "requester_slack_id": "U0EXAMPLE01", "requester_name": "someone",
            "wants_result": True, "result_format": "csv", "engine": "postgres",
            "required_tier": "ro", "bundle_id": None, "origin": "web",
            "unmasked": asked,
        }
        try:
            ex._run(request, None)
        except LookupError:
            pass
        return seen

    return go


def test_asking_as_a_super_admin_writes_the_audit_row(run):
    seen = run(asked=True, super_admin=True)
    assert "audit" in seen, "masking was skipped with no audit row"
    args, _ = seen["audit"]
    assert args[3] == "result_unmasked", f"wrong action: {args[3]}"


def test_losing_super_admin_after_submit_masks_anyway(run):
    """The row still says unmasked. The standing is gone, so it does not apply —
    and nothing is logged as unmasked, because nothing was."""
    seen = run(asked=True, super_admin=False)
    assert "audit" not in seen


def test_standing_alone_is_not_a_request(run):
    """A super-admin who did not ask gets the same masking as everyone else."""
    seen = run(asked=False, super_admin=True)
    assert "audit" not in seen


def test_neither_one_does_nothing(run):
    assert "audit" not in run(asked=False, super_admin=False)


def test_the_audit_row_is_written_before_the_query_runs(run):
    """A run that fails after this point must still leave the record: the
    interesting case for an audit trail is the one that went wrong."""
    seen = run(asked=True, super_admin=True)
    assert "audit" in seen
    assert "fail" in seen, ("this fixture stops at the credential fetch, so a "
                            "failure is expected — the audit row must precede it")


# ---------------------------------------------------------------------------
# the decision cannot be reached from a request field alone
# ---------------------------------------------------------------------------

def test_the_masking_switch_reads_the_verdict_not_the_row():
    """Structural: `_execute_main_statement` must decide from its `unmasked`
    ARGUMENT — the verdict _run computed — and never re-read request["unmasked"]
    for itself. Reading the row there would skip the live super-admin check."""
    import pathlib
    import re
    src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "dba_slack_bot"
           / "executor.py").read_text()
    body = src[src.index("def _execute_main_statement("):]
    body = body[:body.index("\ndef ")]
    assert 'request["unmasked"]' not in body and 'request.get("unmasked")' not in body, (
        "_execute_main_statement reads the intent field directly, which bypasses "
        "the execution-time super-admin check")
    assert re.search(r"None if unmasked else", body), (
        "the masking switch is gone from _execute_main_statement")
