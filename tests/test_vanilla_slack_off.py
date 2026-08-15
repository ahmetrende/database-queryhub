"""Faz 3.2 — the vanilla profile: with Slack unconfigured, every Slack
send no-ops instead of touching a (possibly absent) client. Locks the
runtime guards so a fresh clone runs end-to-end web-only."""
from types import SimpleNamespace

import pytest

from dba_slack_bot import config as cfg
from dba_slack_bot import executor
from dba_slack_bot.slack_app import notifications


class _Boom:
    """A client whose every attribute access fails — proves the guard
    returns BEFORE any Slack call is attempted."""
    def __getattr__(self, name):
        raise AssertionError(f"Slack API touched in vanilla profile: {name}")


@pytest.fixture
def slack_off(monkeypatch):
    monkeypatch.setattr(cfg, "ENV", SimpleNamespace(slack_enabled=False))


def test_notification_senders_noop_without_slack(slack_off):
    assert notifications._post(_Boom(), channel="c", text="t") is None
    assert notifications._update(_Boom(), channel="c", ts="1", text="t") is None
    assert notifications.dm_requester(_Boom(), "U1", "hi") is None
    assert notifications.notify_admins(_Boom(), {"id": 1}) is None


def test_result_delivery_off_without_slack(slack_off):
    # Nothing is delivered to Slack; the web UI serves the result.
    assert executor._deliver_result_to_requester({"origin": "web"}) is False
    assert executor._deliver_result_to_requester({"origin": "slack"}) is False
    # The upload primitive returns an empty response (unreached in practice,
    # but must not touch a None client if it ever is).
    assert executor._upload_with_retry(_Boom()) == {}


def test_web_decision_slack_client_none_without_slack(slack_off):
    """The web approval path calls _slack_client() on every decision. In the
    vanilla profile it must return None BEFORE importing slack_sdk (which may
    not be installed) — otherwise approving a request would 500. apply_effects
    then no-ops every Slack side effect on the None client."""
    from dba_slack_bot.web import routes_admin
    assert routes_admin._slack_client() is None
    # Profile backfill on a new grant likewise no-ops without Slack.
    assert routes_admin._slack_profile("U1") == {}


def test_access_request_fanout_noops_without_slack(slack_off):
    """The one send path that had no guard.

    `client` is None in the vanilla profile, so `client.conversations_open`
    raised AttributeError; the per-admin `except Exception` swallowed it once
    per admin and the function returned None. _Boom() is the same probe used
    above — if the guard is removed, the first attribute access fails the test
    instead of being logged and hidden.
    """
    from dba_slack_bot.slack_app import access
    assert access.fan_out_admin_dms(_Boom(), {"id": 1,
                                              "requester_slack_id": "local:dev"},
                                    None) is None
    # None, the actual vanilla value, must be equally safe.
    assert access.fan_out_admin_dms(None, {"id": 1,
                                           "requester_slack_id": "local:dev"},
                                    None) is None


def test_endpoint_request_is_submitted_not_503_without_slack(slack_off, monkeypatch):
    """The user-visible half of the same bug: the row was saved, the DBA could
    see and approve it, and the requester was told it had failed."""
    from dba_slack_bot import access_requests, admins
    from dba_slack_bot.web import deps, routes_requests

    monkeypatch.setattr(deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(routes_requests, "_target_by_alias", lambda alias: None)
    monkeypatch.setattr(routes_requests, "_bot_client", lambda: None)
    # The abuse guard counts this user's open requests before doing anything.
    monkeypatch.setattr(admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(access_requests, "open_count_for", lambda uid: 0)
    monkeypatch.setattr(access_requests, "create",
                        lambda **kw: {"id": 42, "requester_slack_id": "local:dev"})
    # A vanilla install bootstraps exactly this: create_local_user.py --admin
    # inserts local:<username> into admins.
    monkeypatch.setattr(admins, "list_active",
                        lambda: [{"slack_user_id": "local:alice", "name": "Alice"}])

    body = routes_requests.EndpointRequestIn(
        server="new-cluster", tier="RO", reason="need read access")
    out = routes_requests.endpoint_request(body, claims={"sub": "local:dev",
                                                        "name": "Dev"})
    assert out["status"] == "submitted"
    assert out["id"] == "er_42"
    assert out["slackMessageTs"] is None      # nothing was sent, and that's fine


def test_endpoint_request_still_503s_when_there_are_no_admins(slack_off, monkeypatch):
    """The condition 503 is actually for: saved, but nobody can act on it."""
    from fastapi import HTTPException

    from dba_slack_bot import access_requests, admins
    from dba_slack_bot.web import deps, routes_requests

    monkeypatch.setattr(deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(routes_requests, "_target_by_alias", lambda alias: None)
    monkeypatch.setattr(routes_requests, "_bot_client", lambda: None)
    monkeypatch.setattr(admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(access_requests, "open_count_for", lambda uid: 0)
    monkeypatch.setattr(access_requests, "create",
                        lambda **kw: {"id": 43, "requester_slack_id": "local:dev"})
    monkeypatch.setattr(admins, "list_active", lambda: [])

    body = routes_requests.EndpointRequestIn(
        server="new-cluster", tier="RO", reason="need read access")
    with pytest.raises(HTTPException) as e:
        routes_requests.endpoint_request(body, claims={"sub": "local:dev"})
    assert e.value.status_code == 503
