"""`confirmed=True` is not an escape hatch.

The super-admin path turns the bulk-destructive refusals into a question. That
question travels to the client and comes back as `confirmed=True`, which makes it
the one piece of this feature a caller can set — so the tests that matter most
here are the ones proving it buys nothing on its own:

  * an ordinary user sending confirmed=True is still refused
  * a super-admin is still refused for the one thing that is never negotiable
    (changing the logging settings), confirmed or not

Everything is derived from `admins.is_super_admin`, which reads the database on
every submission. These tests stub that function — that is the seam an attacker
would need to control, and controlling it means already being in the process.
"""
from __future__ import annotations

import pytest

from queryhub import core_submit as cs
from queryhub import lifecycle, targets

WHERELESS = "UPDATE rewards SET flag = 1"
KILLS_AUDIT = "ALTER DATABASE reward_service SET log_statement = 'none'"


@pytest.fixture
def env(monkeypatch):
    """Enough of the submit path to reach the safety gate, and no further."""
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: True)  # skip rate limit
    monkeypatch.setattr(cs.cfg, "get_int", lambda k, d=None: d if d is not None else 5)
    monkeypatch.setattr(cs.cfg, "get_setting", lambda k, d=None: d)
    monkeypatch.setattr(cs.cfg, "get_bool", lambda k, d=False: d)
    target = targets.TargetServer(
        id=7, alias="t", host="h", port=5432, default_database="db",
        username="u", enabled=True, notes=None, engine="postgres")
    monkeypatch.setattr(cs.targets, "get", lambda tid: target)

    def _must_not_reach(*a, **k):
        raise AssertionError("reached past the safety gate")

    monkeypatch.setattr(cs.query_safety, "required_mode", _must_not_reach)
    monkeypatch.setattr(cs.teams, "effective_grant_for_user", _must_not_reach)
    return monkeypatch


def _submit(sql, **kw):
    return cs.validate_submission(
        "U0EXAMPLE01", "someone", target_server_id=7, database_name="db",
        query=sql, justification="because", **kw)


def _as(monkeypatch, *, super_admin: bool):
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: super_admin)


# ---------------------------------------------------------------------------
# the ordinary user is unaffected in both directions
# ---------------------------------------------------------------------------

def test_an_ordinary_user_is_still_refused(env):
    _as(env, super_admin=False)
    out = _submit(WHERELESS)
    assert isinstance(out, cs.Rejection)
    assert out.field == "query", "an ordinary user must get the refusal, not a prompt"
    assert "is blocked" in out.message


def test_confirmed_true_gains_an_ordinary_user_nothing(env):
    """The attack: send the flag the super-admin flow sends."""
    _as(env, super_admin=False)
    out = _submit(WHERELESS, confirmed=True)
    assert isinstance(out, cs.Rejection)
    assert out.field == "query", (
        "confirmed=True let a non-super-admin past a blocker — the flag is "
        "supposed to answer a question, not remove one")
    assert out.reason != "needs_confirmation"


# ---------------------------------------------------------------------------
# the super-admin gets a question, then gets through
# ---------------------------------------------------------------------------

def test_a_super_admin_is_asked_rather_than_refused(env):
    _as(env, super_admin=True)
    out = _submit(WHERELESS)
    assert isinstance(out, cs.Rejection)
    assert out.field == "confirm"
    assert out.reason == "needs_confirmation"
    assert "every row" in out.message


def test_confirming_gets_the_super_admin_past_the_gate(env):
    _as(env, super_admin=True)
    with pytest.raises(AssertionError, match="reached past the safety gate"):
        _submit(WHERELESS, confirmed=True)


def test_an_ordinary_statement_never_asks(env):
    _as(env, super_admin=True)
    with pytest.raises(AssertionError, match="reached past the safety gate"):
        _submit("UPDATE rewards SET flag = 1 WHERE id = 5")


# ---------------------------------------------------------------------------
# the one thing confirmation cannot buy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("confirmed", [False, True])
def test_the_audit_killer_is_refused_even_for_a_super_admin(env, confirmed):
    _as(env, super_admin=True)
    out = _submit(KILLS_AUDIT, confirmed=confirmed)
    assert isinstance(out, cs.Rejection)
    assert out.field == "query", (
        "a statement that turns logging off came back as a confirmable prompt")
    assert "logging" in out.message


# ---------------------------------------------------------------------------
# the flag reaches the classifier, and only from the identity check
# ---------------------------------------------------------------------------

def test_the_identity_is_what_drives_the_classifier(env):
    """Pin the wiring: analyze() must be called with unrestricted set from
    is_super_admin, not from anything in the submission."""
    seen = {}
    real = cs.query_safety.analyze

    def spy(sql, engine="postgres", unrestricted=False):
        seen["unrestricted"] = unrestricted
        return real(sql, engine=engine, unrestricted=unrestricted)

    env.setattr(cs.query_safety, "analyze", spy)

    _as(env, super_admin=False)
    _submit(WHERELESS)
    assert seen["unrestricted"] is False

    _as(env, super_admin=True)
    _submit(WHERELESS)
    assert seen["unrestricted"] is True
