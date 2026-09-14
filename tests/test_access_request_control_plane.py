"""The access-request flow may not hand out the bot's own database.

`grants.grant` and `grants.grant_many` have refused the control-plane target
since the DDL-only elevation work, and the web endpoint-request route refuses
it at submission. The Slack access-request flow had neither check and does not
call `grants.grant` at all: it writes `user_target_grants` directly. The target
picker offers it too -- `handle_access_target_options` returns ALL enabled
targets on purpose, because somebody asking for access is by definition asking
for something they cannot reach.

Nothing had gone wrong in production: the only live grant on that target is the
deliberate read-only one. The door was simply open, and a grant there is not an
ordinary over-grant -- that database holds the audit log, the grant tables and
the config deciding who may approve.

Two shapes, deliberately different. At submission the person is holding the
form, so `create()` RAISES and they are told. At approval the admin is looking
at a card that already renders "auto-grant skipped, and why", so `_auto_grant`
returns a reason -- and a request made before this guard existed stays
decidable, which it must be, or nobody can reject it.
"""
import inspect

import pytest

from queryhub import access_requests


def test_creating_a_request_for_the_control_plane_is_refused(monkeypatch):
    monkeypatch.setattr("queryhub.grants.control_plane_target_ids",
                        lambda: {1})
    with pytest.raises(access_requests.ControlPlaneRefused):
        access_requests.create(
            principal_id="U1", name="somebody", target_server_id=1,
            database_name=None, attempted_query=None, reason="please")


def test_an_ordinary_target_is_not_refused(monkeypatch):
    """The guard must not become a gate on everything."""
    monkeypatch.setattr("queryhub.grants.control_plane_target_ids",
                        lambda: {1})
    access_requests._refuse_control_plane(52)      # does not raise
    access_requests._refuse_control_plane(None)    # nor on a target-less ask


def test_the_grant_write_refuses_as_a_reason_not_an_exception():
    """`decide()` must keep working for a request that predates the guard:
    an admin has to be able to reject it. Raising there would leave such a
    request undecidable from either surface."""
    src = inspect.getsource(access_requests._auto_grant)
    assert '"reason": "control_plane"' in src
    assert '"applied": False' in src
    # Comments strippped: the prose above the guard says the word "raise"
    # while explaining why it does not. Asserting on the file caught that
    # instead of the code.
    code = "\n".join(ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#"))
    assert "raise" not in code


def test_the_refusal_is_asked_of_grants_not_hardcoded():
    """Target 1 is the control plane in this deployment and not in general;
    `grants.control_plane_target_ids()` reads config first and falls back to
    detection. A literal here would be wrong on any other install."""
    src = inspect.getsource(access_requests._refuse_control_plane)
    assert "grants.control_plane_target_ids()" in src
    assert "== 1" not in src and "in (1," not in src


def test_the_approve_card_names_the_refusal():
    """An admin who presses Approve and sees nothing happen will press it
    again. The card already explains tier_conflict and no_target the same way."""
    from queryhub.slack_app import handlers
    src = inspect.getsource(handlers)
    assert 'ag.get("reason") == "control_plane"' in src
    assert "control-plane database" in src


def test_the_slack_modal_tells_the_person_instead_of_crashing():
    """The picker deliberately offers every enabled target, so the refusal is
    the only place the person learns; an exception into Bolt would show them
    nothing at all."""
    from queryhub.slack_app import handlers
    src = inspect.getsource(handlers)
    assert "except access_requests.ControlPlaneRefused" in src
