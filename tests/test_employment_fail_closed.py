"""A write needs Slack to say, now, that the person still works here.

External review 2026-09-24 (SEC-06): the Slack employment check passed everyone
on a transport error. The access token lives 20 minutes but a refresh session
12 hours, so while Slack could not be reached an offboarded person whose rows
had not been removed yet kept refreshing, and kept submitting writes, for as
long as the outage lasted.

Now there are three answers. "gone" refuses everywhere, as before. "unknown"
refuses a write outright, and passes a sign-in or refresh only if Slack called
the person active within `web_employment_grace_hours`. The check also runs for
every provider but `local`: the RW/DDL gate tested `provider == "slack"`, so a
company-SSO session skipped it.
"""
import pytest

from queryhub import config as cfg
from queryhub.web import deps, routes_queries

UID = "U0EXAMPLE001"


class _SlackApiError(Exception):
    def __init__(self, code):
        super().__init__(f"The request to the Slack API failed. error: {code}")
        self.response = {"ok": False, "error": code}


@pytest.fixture
def slack(monkeypatch):
    """users.info answers with whatever `st["answer"]` holds."""
    import slack_sdk
    import slack_sdk.errors

    st = {"answer": {"user": {"id": UID, "deleted": False}}, "writes": [], "vouched": False}
    monkeypatch.setattr(slack_sdk.errors, "SlackApiError", _SlackApiError)

    class _Client:
        def __init__(self, token=None, timeout=None):
            st["timeout"] = timeout

        def users_info(self, user):
            a = st["answer"]
            if isinstance(a, Exception):
                raise a
            return a
    monkeypatch.setattr(slack_sdk, "WebClient", _Client)
    monkeypatch.setattr(deps.db, "execute", lambda sql, params=None: st["writes"].append(params))
    monkeypatch.setattr(deps.db, "fetch_one",
                        lambda sql, params=None: {"ok": 1} if st["vouched"] else None)
    return st


def test_an_active_account_is_recorded(slack):
    assert deps.slack_employment(UID) == "active"
    assert slack["writes"] == [(UID,)]
    assert slack["timeout"] == 5


@pytest.mark.parametrize("answer", [
    {"user": {"id": UID, "deleted": True}},
    _SlackApiError("user_not_found"),
    _SlackApiError("users_not_found"),
    _SlackApiError("user_not_visible"),
])
def test_a_definitive_answer_is_gone(slack, answer):
    """`user_not_found` is users.info's own code. The old substring test looked
    for `users_not_found`, lookupByEmail's spelling, and so passed it."""
    slack["answer"] = answer
    assert deps.slack_employment(UID) == "gone"
    assert slack["writes"] == []


@pytest.mark.parametrize("answer", [
    _SlackApiError("ratelimited"), _SlackApiError("invalid_auth"),
    TimeoutError("read timed out"), OSError("network unreachable"),
])
def test_anything_else_says_nothing_about_the_person(slack, answer):
    slack["answer"] = answer
    assert deps.slack_employment(UID) == "unknown"


def test_no_slack_workspace_means_the_row_is_the_whole_check(monkeypatch):
    import dataclasses
    monkeypatch.setattr(deps.cfg, "ENV", dataclasses.replace(cfg.ENV, slack_bot_token=""))
    assert deps.slack_employment(UID) == "active"


def test_sign_in_and_refresh_pass_on_a_recent_answer_only(slack):
    slack["answer"] = TimeoutError()
    assert deps.employment_verdict(UID) == "unconfirmed"
    slack["vouched"] = True
    assert deps.employment_verdict(UID) == "active"
    slack["answer"] = {"user": {"deleted": True}}
    assert deps.employment_verdict(UID) == "gone"


@pytest.mark.parametrize("provider,checked", [
    ("slack", True), ("corpsso", True), ("idp", True), ("local", False), (None, False)])
def test_every_provider_but_local_is_checked(provider, checked):
    assert deps.employment_checked({"provider": provider}) is checked


# --- the write gate -----------------------------------------------------------


@pytest.fixture
def gate(monkeypatch):
    st = {"state": "active", "revoked": []}
    monkeypatch.setattr(routes_queries.deps, "slack_employment", lambda uid: st["state"])
    monkeypatch.setattr(routes_queries.sessions, "revoke_user",
                        lambda uid, why: st["revoked"].append(why))
    return st


def test_a_write_passes_on_a_live_active_answer(gate):
    routes_queries._employment_gate(UID, "RW/DDL submit")
    assert gate["revoked"] == []


def test_a_write_by_someone_gone_ends_every_session(gate):
    gate["state"] = "gone"
    with pytest.raises(Exception) as e:
        routes_queries._employment_gate(UID, "RW/DDL submit")
    assert e.value.status_code == 401 and gate["revoked"]


def test_a_write_without_an_answer_is_refused_and_the_sessions_stay(gate):
    """An outage says nothing about the person, so nobody is signed out. A
    write still needs the answer, and does not take an old one."""
    gate["state"] = "unknown"
    with pytest.raises(Exception) as e:
        routes_queries._employment_gate(UID, "RW/DDL submit")
    assert e.value.status_code == 503 and gate["revoked"] == []


def test_both_submit_paths_use_the_gate_for_every_provider():
    import inspect
    src = inspect.getsource(routes_queries)
    assert src.count("_employment_gate(uid,") == 2
    assert 'claims.get("provider") == "slack"' not in src
    assert src.count("deps.employment_checked(claims)") == 2


def test_the_grace_setting_is_seeded():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "migrations"
            / "134_slack_liveness.sql").read_text(encoding="utf-8")
    assert "('web_employment_grace_hours', '2'," in text
    assert "CREATE TABLE IF NOT EXISTS slack_liveness" in text
