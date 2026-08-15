"""Auto-approve FYI routing: RO → quiet feed channel, RW/DDL → admin DMs,
with DM fallback when the channel is unset or the post fails."""
import pytest

from queryhub.slack_app import notifications


class _Client:
    def __init__(self):
        self.opened = []

    def conversations_open(self, users=None):
        self.opened.append(users)
        return {"channel": {"id": f"D_{users}"}}


@pytest.fixture
def wired(monkeypatch):
    """Wire up notifications' collaborators; return (posts, set_feed, set_admins)."""
    posts = []
    state = {"feed": None, "admins": ["A1", "A2", "A3"], "raise_on": None}

    def fake_get_setting(key, default=None):
        if key == "auto_approve_feed_channel":
            return state["feed"] or ""
        return default if default is not None else ""

    def fake_post(client, **kw):
        if state["raise_on"] and kw.get("channel") == state["raise_on"]:
            raise RuntimeError("channel post boom")
        posts.append(kw)
        return {"ts": "1.1"}

    monkeypatch.setattr(notifications.cfg, "get_setting", fake_get_setting)
    monkeypatch.setattr(notifications.admins, "list_active",
                        lambda: [{"slack_user_id": a} for a in state["admins"]])
    monkeypatch.setattr(notifications, "display_overrides", lambda: {})
    monkeypatch.setattr(notifications, "_post", fake_post)
    monkeypatch.setattr(notifications, "query_preview_block",
                        lambda q: {"type": "section",
                                   "text": {"type": "mrkdwn", "text": "q"}})
    return posts, state


def _channels(posts):
    return [p["channel"] for p in posts]


def test_ro_routes_to_feed_channel_no_dms(wired):
    posts, state = wired
    state["feed"] = "C_FEED"
    client = _Client()
    notifications.deliver_auto_approve_fyi(
        client, {"id": 1, "query": "select 1"}, "hdr", quiet=True)
    assert _channels(posts) == ["C_FEED"]   # one post, to the channel
    assert client.opened == []              # no per-admin DM fan-out


def test_ro_without_feed_channel_falls_back_to_dms(wired):
    posts, state = wired
    state["feed"] = None
    client = _Client()
    notifications.deliver_auto_approve_fyi(
        client, {"id": 2, "query": "select 1"}, "hdr", quiet=True)
    assert _channels(posts) == ["D_A1", "D_A2", "D_A3"]


def test_rw_ddl_always_dms_even_with_feed_channel(wired):
    posts, state = wired
    state["feed"] = "C_FEED"
    client = _Client()
    notifications.deliver_auto_approve_fyi(
        client, {"id": 3, "query": "update t set a=1"}, "hdr", quiet=False)
    assert _channels(posts) == ["D_A1", "D_A2", "D_A3"]
    assert "C_FEED" not in _channels(posts)


def test_channel_failure_falls_back_to_dms(wired):
    posts, state = wired
    state["feed"] = "C_FEED"
    state["raise_on"] = "C_FEED"    # channel post throws
    client = _Client()
    notifications.deliver_auto_approve_fyi(
        client, {"id": 4, "query": "select 1"}, "hdr", quiet=True)
    # channel attempt raised (not recorded) → DM fan-out took over
    assert _channels(posts) == ["D_A1", "D_A2", "D_A3"]


def test_grant_ro_is_quiet_rw_is_not(wired):
    posts, state = wired
    state["feed"] = "C_FEED"

    class _T:
        alias = "t"
    notifications.dm_admins_auto_approved(
        _Client(), {"id": 5, "query": "select 1", "database_name": "d",
                    "requester_slack_id": "U"}, _T(),
        {"id": 9, "max_tier": "ro"})
    assert _channels(posts) == ["C_FEED"]

    posts.clear()
    notifications.dm_admins_auto_approved(
        _Client(), {"id": 6, "query": "drop table t", "database_name": "d",
                    "requester_slack_id": "U"}, _T(),
        {"id": 10, "max_tier": "ddl"})
    assert _channels(posts) == ["D_A1", "D_A2", "D_A3"]
