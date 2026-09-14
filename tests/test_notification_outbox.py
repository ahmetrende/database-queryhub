"""notification_outbox: the row the IDP panel polls to learn a request is
waiting for approval (migrations/102_notification_outbox.sql).

Ruling P-6 (see .superpowers/sdd ledger in the idp repo) says the outbox row
must be written in `dispatch_and_notify`, from the SAME `admins.list_active()`
result that drives the Slack DM fan-out — not a second, independently-derived
list. That makes the two recipient lists equal *by construction*. A test that
only compares the two lists' contents would therefore prove less than it
sounds like it proves (of course two views of the same list are equal); what
actually matters is that there is only ONE call to `admins.list_active()` in
the pending path, so the two are structurally the same list rather than two
calls that happen to agree. This file asserts both: the values, and the call
count that makes the value-equality meaningful rather than coincidental.
"""
from __future__ import annotations

import json

import pytest

from queryhub import core_submit as cs
from queryhub import targets
from queryhub.slack_app import notifications as notifications_mod


def _target():
    return targets.TargetServer(
        id=7, alias="prod-primary", host="h", port=5432, default_database="db",
        username="u", enabled=True, notes=None, engine="postgres")


def _prep(**overrides):
    kwargs = dict(
        user_id="U0REQUESTER", user_name="Ada", target=_target(),
        database="db", query="SELECT 1", required_mode="RO",
        justification="need it", wants_result=True, result_format="table",
        sched_for=None, explain_plan=None, risk_summary=None, origin="web",
    )
    kwargs.update(overrides)
    return cs.Prepared(**kwargs)


def _outcome(row):
    return cs.Outcome(row=row, auto_approved=False)


def _row():
    return {
        "id": 42,
        "requester_slack_id": "U0REQUESTER",
        "requester_name": "Ada",
        "target_server_id": 7,
        "database_name": "db",
        "query": "SELECT 1",
        "justification": "need it",
        "required_tier": "RO",
        "status": "pending",
        "scheduled_for": None,
    }


@pytest.fixture
def active_admins():
    return [
        {"slack_user_id": "U0ADMIN1", "name": "Bea", "source": "permanent", "expires_at": None},
        {"slack_user_id": "U0ADMIN2", "name": "Cy", "source": "temp", "expires_at": None},
    ]


@pytest.fixture
def env(monkeypatch, active_admins):
    """Enough of dispatch_and_notify's pending path to reach the outbox
    write, with every side effect captured instead of performed."""
    calls = {"list_active": 0, "notify_admins_args": None, "db_execute": []}

    def _list_active():
        calls["list_active"] += 1
        return active_admins

    def _fake_notify_admins(client, request, *args, **kwargs):
        calls["notify_admins_args"] = (args, kwargs)

    def _fake_execute(sql, params=None):
        calls["db_execute"].append((sql, params))

    monkeypatch.setattr(cs.admins, "list_active", _list_active)
    monkeypatch.setattr(notifications_mod, "notify_admins", _fake_notify_admins)
    monkeypatch.setattr(cs.db, "execute", _fake_execute)
    return calls


def _outbox_calls(calls):
    return [c for c in calls["db_execute"] if "notification_outbox" in c[0]]


def test_pending_request_writes_exactly_one_outbox_row(env):
    result = cs.dispatch_and_notify(object(), _prep(), _outcome(_row()), dm_requester=False)

    assert result == "pending"
    outbox_calls = _outbox_calls(env)
    assert len(outbox_calls) == 1, (
        f"expected exactly one notification_outbox insert, got {len(outbox_calls)}"
    )


def test_outbox_recipients_are_the_single_list_active_result_notify_admins_used(env, active_admins):
    """Proves the two recipient lists are the SAME list (Ruling P-6), not two
    independently-correct derivations that happen to agree. The call-count
    assertion is what makes that structural claim rather than a coincidence:
    with only one `admins.list_active()` call in `dispatch_and_notify`, there
    is only one list for both the DM fan-out and the outbox row to disagree
    about.

    What this test CANNOT catch: `notify_admins` itself is replaced wholesale
    by `env`'s fake, so a regression inside notify_admins's own body — e.g.
    reverting its `admin_list if admin_list is not None else
    admins.list_active()` branch back to an unconditional
    `admins.list_active()` call, silently reintroducing the second
    independent call P-6 exists to prevent — runs none of this file's
    assertions and would not go red here. That real-body branch is exercised
    separately, by `test_notify_admins_iterates_the_given_admin_list`.
    """
    cs.dispatch_and_notify(object(), _prep(), _outcome(_row()), dm_requester=False)

    assert env["list_active"] == 1, (
        "admins.list_active() must be called exactly once in the pending "
        "path — a second call is a second source of truth for recipients"
    )

    notify_args, notify_kwargs = env["notify_admins_args"]
    dm_recipients = [a["slack_user_id"] for a in (notify_args[0] if notify_args
                                                    else notify_kwargs["admin_list"])]

    sql, params = _outbox_calls(env)[0]
    outbox_recipients = params[2]  # (event_type, request_id, recipients, payload)

    assert outbox_recipients == dm_recipients == [a["slack_user_id"] for a in active_admins]


def test_outbox_row_shape(env):
    cs.dispatch_and_notify(object(), _prep(), _outcome(_row()), dm_requester=False)

    sql, params = _outbox_calls(env)[0]
    event_type, request_id, recipients, payload_json = params
    assert event_type == "queryhub.request_pending"
    assert request_id == 42
    payload = json.loads(payload_json)
    # requestId is "42" (str), not 42 (int) -- see Ruling P-10 and
    # test_outbox_payload_values_are_all_strings below for why that
    # distinction is load-bearing rather than cosmetic.
    assert payload == {
        "requesterName": "Ada",
        "tier": "RO",
        "target": "prod-primary",
        "justification": "need it",
        "requestId": "42",
    }


def test_outbox_payload_values_are_all_strings(env):
    """Ruling P-10: the panel decodes `payload` into Go's
    map[string]string. A bare JSON number (requestId was written as a
    Python int before this test existed) or a JSON null (tier /
    justification, when unset) fails Go's json.Unmarshal for the WHOLE
    outbox response, not just that one field -- so one pending request with
    an unset justification silently stopped every notification, to every
    admin, from ever being delivered. Both repos' test suites were green
    throughout, because each side only ever exercised its own fixture.

    Covers both the common case (a fully-populated row) and the case that
    actually broke (required_tier and justification absent) in one test,
    since the assertion -- every value is `str` -- is the same either way.
    """
    cs.dispatch_and_notify(object(), _prep(), _outcome(_row()), dm_requester=False)
    sql, params = _outbox_calls(env)[0]
    payload = json.loads(params[3])
    for key, value in payload.items():
        assert isinstance(value, str), (
            f"payload[{key!r}] = {value!r} ({type(value).__name__}) is not "
            "a string -- Go's map[string]string cannot decode it, and the "
            "failure is whole-response, not per-field"
        )

    # The row that actually broke this: no requester_name, no tier, no
    # justification. None of the four must come back as JSON null.
    sparse_row = _row()
    sparse_row["requester_name"] = None
    sparse_row["required_tier"] = None
    sparse_row["justification"] = None
    cs.dispatch_and_notify(object(), _prep(), _outcome(sparse_row), dm_requester=False)
    sql, params = _outbox_calls(env)[-1]
    sparse_payload = json.loads(params[3])
    for key, value in sparse_payload.items():
        assert isinstance(value, str), (
            f"payload[{key!r}] = {value!r} ({type(value).__name__}) is not "
            "a string with an absent source field"
        )
    # requesterName falls back to requester_slack_id when the name is None;
    # tier and justification have no further fallback and must be "".
    assert sparse_payload["requesterName"] == "U0REQUESTER"
    assert sparse_payload["tier"] == ""
    assert sparse_payload["justification"] == ""


def test_no_admins_writes_no_outbox_row(env, monkeypatch):
    """The existing no_admins short-circuit must not leave a dangling row —
    there is nobody to notify, in-app or otherwise."""
    monkeypatch.setattr(cs.admins, "list_active", lambda: [])
    result = cs.dispatch_and_notify(object(), _prep(), _outcome(_row()), dm_requester=False)

    assert result == "no_admins"
    assert _outbox_calls(env) == []


def test_outbox_write_failure_does_not_raise(env, monkeypatch):
    """The request already exists and admins are already DMed by the time the
    outbox insert runs. `web/routes_queries.py` calls dispatch_and_notify
    with no guard of its own, after create_request has already committed —
    so an unguarded exception here would turn a SUCCESSFUL submission into a
    client-visible 500. A lost outbox row is recoverable (the approvals
    queue still shows the request); a false 500 is not."""
    def _boom(sql, params=None):
        raise RuntimeError("db blip")
    monkeypatch.setattr(cs.db, "execute", _boom)

    result = cs.dispatch_and_notify(object(), _prep(), _outcome(_row()), dm_requester=False)

    assert result == "pending"
    # And the DM fan-out — the thing that actually reaches a human right
    # now — still happened despite the outbox write failing.
    assert env["notify_admins_args"] is not None


class _FakeAdminClient:
    """A minimal Slack client for notify_admins's REAL body — records what
    it was asked to do instead of talking to Slack."""

    def __init__(self):
        self.opened_for: list[str] = []
        self.posted: list[dict] = []

    def conversations_open(self, users):
        self.opened_for.append(users)
        return {"channel": {"id": f"C-{users}"}}

    def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ts": "123.456"}


def test_notify_admins_iterates_the_given_admin_list(monkeypatch):
    """Exercises `notify_admins`'s REAL body (every other test in this file
    replaces it wholesale with a fake), with a non-None `admin_list`, and
    proves the given list — not a fresh `admins.list_active()` call — is
    what gets iterated. `admins.list_active` is poisoned to fail the test
    loudly if the branch is ever reverted to call it unconditionally, which
    is exactly the regression Ruling P-6 exists to prevent."""
    from types import SimpleNamespace

    monkeypatch.setattr(notifications_mod.cfg, "ENV",
                         SimpleNamespace(slack_enabled=True))
    monkeypatch.setattr(notifications_mod.targets, "get", lambda tid: _target())
    monkeypatch.setattr(notifications_mod, "_request_blocks", lambda *a, **k: [])
    monkeypatch.setattr(notifications_mod.admins, "can_approve", lambda aid, req: False)
    monkeypatch.setattr(notifications_mod.db, "execute", lambda *a, **k: None)

    def _list_active_must_not_be_called():
        raise AssertionError(
            "notify_admins called admins.list_active() even though a "
            "non-None admin_list was given — Ruling P-6 requires the "
            "caller's list to win")
    monkeypatch.setattr(notifications_mod.admins, "list_active",
                         _list_active_must_not_be_called)

    given = [
        {"slack_user_id": "U0GIVEN1", "name": "Given One",
         "source": "permanent", "expires_at": None},
        {"slack_user_id": "U0GIVEN2", "name": "Given Two",
         "source": "permanent", "expires_at": None},
    ]
    client = _FakeAdminClient()

    notifications_mod.notify_admins(client, _row(), given)

    assert client.opened_for == ["U0GIVEN1", "U0GIVEN2"]
