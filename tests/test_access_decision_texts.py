"""What an access-request decision says, and who hears it.

The Slack buttons and the QueryHub Web screen both decide a request. The words
used to live in the Slack handler only, so a decision made on the screen told
nobody. They live in `slack_app/access.py` now, and these tests pin them: the
text of every branch is the text the Slack handler sent before the move.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from queryhub.slack_app import access, notifications


@pytest.fixture(autouse=True)
def _target_names(monkeypatch):
    """`access_context_md` looks the target up. Answer without a database."""
    monkeypatch.setattr(
        access.targets, "get",
        lambda tid: SimpleNamespace(alias="demo-orders", id=tid, host="h.example"))


def _row(**over):
    row = {"id": 17, "requester_slack_id": "U0REQUESTER",
           "target_server_id": 52, "database_name": None,
           "reason": "need one look", "attempted_query": None}
    row.update(over)
    return row


WHO = "<@U0DECIDER>"


# ---- the end date in words --------------------------------------------------

def test_until_reads_a_datetime_as_utc():
    assert access._until(datetime(2026, 10, 14, 11, 9, 29,
                                  tzinfo=timezone.utc)) == "2026-10-14 11:09 UTC"


def test_until_converts_another_zone_to_utc():
    istanbul = timezone(timedelta(hours=3))
    assert access._until(datetime(2026, 10, 14, 14, 9,
                                  tzinfo=istanbul)) == "2026-10-14 11:09 UTC"


def test_until_takes_a_naive_datetime_as_utc_and_an_iso_string():
    assert access._until(datetime(2026, 10, 14, 11, 9)) == "2026-10-14 11:09 UTC"
    assert access._until("2026-10-14T11:09:29+00:00") == "2026-10-14 11:09 UTC"


def test_until_is_empty_for_no_end_date():
    assert access._until(None) == ""
    assert access._until("") == ""


# ---- approval ---------------------------------------------------------------

def test_approved_with_a_grant():
    ag = {"applied": True, "reason": "granted", "mode": "ro",
          "databases": None, "expires_at": None}
    status, dm = access.approval_texts(_row(auto_grant=ag), WHO)
    assert status == (":white_check_mark: Approved by <@U0DECIDER>\n"
                      ":key: Granted automatically: *RO* on _all databases_.")
    assert dm.startswith(
        ":white_check_mark: *Access request `#17` approved* by <@U0DECIDER>.\n"
        "*Target:* `demo-orders`")
    assert dm.endswith("\n\nYou can now run `/sql` — your access is active.")


def test_approved_names_the_databases():
    ag = {"applied": True, "mode": "rw", "databases": ["a", "b"]}
    status, _ = access.approval_texts(_row(auto_grant=ag), WHO)
    assert "*RW* on `a`, `b`." in status


def test_approved_with_an_end_date_says_so_to_both():
    end = datetime(2026, 10, 14, 11, 9, tzinfo=timezone.utc)
    ag = {"applied": True, "mode": "ro", "databases": None, "expires_at": end}
    status, dm = access.approval_texts(_row(auto_grant=ag), WHO)
    assert "on _all databases_, until `2026-10-14 11:09 UTC`." in status
    assert dm.endswith("your access is active. It lasts until "
                       "`2026-10-14 11:09 UTC`.")


def test_approved_but_the_tier_conflicts():
    ag = {"applied": False, "reason": "tier_conflict", "mode": "rw"}
    status, dm = access.approval_texts(_row(auto_grant=ag), WHO)
    assert ("\n:warning: Auto-grant skipped: an active grant at a different "
            "tier (*RW*) already exists. To give the requested tier, change "
            "that grant manually.") in status
    assert dm.endswith("\n\nA DBA will finalize your access shortly.")


def test_approved_but_the_target_is_the_control_plane():
    status, dm = access.approval_texts(
        _row(auto_grant={"applied": False, "reason": "control_plane"}), WHO)
    assert "\n:no_entry: Auto-grant refused: this is the bot's own " in status
    assert dm.endswith("\n\nAn access request cannot grant this connection.")


def test_approved_but_the_server_is_not_a_target():
    status, dm = access.approval_texts(
        _row(auto_grant={"applied": False, "reason": "no_target"}), WHO)
    assert ("\n:warning: Auto-grant skipped: this server is not a target yet. "
            "Onboard it, then grant access manually.") in status
    assert dm.endswith("\n\nA DBA will finalize your access shortly.")


def test_approved_with_no_summary_at_all():
    status, dm = access.approval_texts(_row(), WHO)
    assert status == ":white_check_mark: Approved by <@U0DECIDER>"
    assert dm.endswith("\n\nYou can now run `/sql`.")


def test_the_decider_can_be_a_plain_name():
    """A local account has no Slack id. A mention of it would show as text."""
    status, dm = access.approval_texts(_row(), "Ops Person")
    assert status == ":white_check_mark: Approved by Ops Person"
    assert "approved* by Ops Person." in dm


# ---- rejection --------------------------------------------------------------

def test_rejected_carries_the_reason_to_both():
    status, dm = access.rejection_texts(_row(), WHO, "too broad")
    assert status == ":x: Rejected by <@U0DECIDER> — too broad"
    assert dm.startswith(
        ":x: *Access request `#17` rejected* by <@U0DECIDER>\n"
        "*Target:* `demo-orders`")
    assert dm.endswith("\n*Reason:* too broad")


# ---- who hears it -----------------------------------------------------------

@pytest.fixture
def slack(monkeypatch):
    log = SimpleNamespace(updates=[], dms=[], order=[])
    monkeypatch.setattr("queryhub.access_requests.get",
                        lambda rid: _row(id=rid, status="approved"))
    monkeypatch.setattr(
        "queryhub.access_requests.list_admin_dms",
        lambda rid: [{"channel_id": "D1", "message_ts": "1.1"},
                     {"channel_id": "D2", "message_ts": "2.2"}])

    def fake_update(client, **kw):
        log.order.append("update")
        if kw["channel"] == "D1" and getattr(log, "fail_first", False):
            raise RuntimeError("slack said no")
        log.updates.append(kw)

    monkeypatch.setattr(notifications, "_update", fake_update)
    monkeypatch.setattr(
        notifications, "dm_requester",
        lambda client, who, text, **kw: (log.order.append("dm"),
                                         log.dms.append((who, text))))
    return log


def test_every_admin_card_loses_its_buttons(slack):
    access.update_admin_cards("C", 17, None, "Approved")
    assert [u["channel"] for u in slack.updates] == ["D1", "D2"]
    for u in slack.updates:
        assert u["text"] == "Approved"
        assert all(b["type"] != "actions" for b in u["blocks"])
        assert u["blocks"][-1]["elements"][0]["text"] == "Approved"


def test_one_card_that_fails_does_not_stop_the_others(slack):
    slack.fail_first = True
    access.update_admin_cards("C", 17, None, "Approved")
    assert [u["channel"] for u in slack.updates] == ["D2"]


def test_the_cards_are_retired_before_the_requester_is_told(slack):
    access.announce_decision("C", _row(), None, "Approved", "Hello")
    assert slack.order == ["update", "update", "dm"]
    assert slack.dms == [("U0REQUESTER", "Hello")]
