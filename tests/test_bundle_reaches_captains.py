"""A batch reaches a pod captain when every item is one they could approve.

Reported 2026-09-24: a captain was DM'd each RO request from their pod, but an
all-RO batch (`/sql batch`) went to the admins alone. Single requests take
their recipients from `admins.notify_list`, which adds a scoped approver when
the request is in their scope. The batch fan-out walked `admins.list_active()`,
which is admins only. The bulk buttons were admin-only too, so a captain who
had been told could still not have used them.

The rule now, the operator's: an approver is listed for a batch when they can
approve EVERY item. A captain's role reaches RO on their own pod's servers, so
for them that is an all-RO batch from their pod. The bulk buttons admit the
same people, and still act only on the items in the presser's scope.
"""
import inspect

import pytest

from queryhub import admins
from queryhub.slack_app import handlers, notifications

ADMIN = {"slack_user_id": "U0EXAMPLE001", "name": "An Admin", "source": "permanent", "expires_at": None}
CAPTAIN = {"slack_user_id": "U0EXAMPLE002", "name": "A Captain", "source": "permanent", "expires_at": None}
OTHER = {"slack_user_id": "U0EXAMPLE003", "name": "Other Captain", "source": "permanent", "expires_at": None}

ITEMS = [{"id": 1, "required_tier": "ro"}, {"id": 2, "required_tier": "ro"}]


@pytest.fixture
def roles(monkeypatch):
    """Admins, approvers, and who may approve which item."""
    st = {"admins": [ADMIN], "approvers": [CAPTAIN, OTHER],
          "can": {"U0EXAMPLE002": {1, 2}, "U0EXAMPLE003": {1}}, "v2": True}
    monkeypatch.setattr(admins, "list_active", lambda: list(st["admins"]))
    monkeypatch.setattr(admins, "_live_approvers", lambda: list(st["approvers"]))
    monkeypatch.setattr(admins, "_v2", lambda: st["v2"])
    monkeypatch.setattr(admins, "can_approve",
                        lambda uid, it: it["id"] in st["can"].get(uid, set()))
    return st


def test_an_approver_who_can_clear_every_item_is_told(roles):
    got = [p["slack_user_id"] for p in admins.notify_list_bundle(ITEMS)]
    assert got == ["U0EXAMPLE001", "U0EXAMPLE002"]


def test_one_item_outside_their_scope_keeps_the_batch_with_the_admins(roles):
    """The other captain can approve item 1 and not item 2 (a write, or
    another pod's server), so the batch is not theirs."""
    got = [p["slack_user_id"] for p in admins.notify_list_bundle(ITEMS)]
    assert "U0EXAMPLE003" not in got


def test_admins_are_listed_for_every_batch(roles):
    roles["can"] = {}
    assert [p["slack_user_id"] for p in admins.notify_list_bundle(ITEMS)] == ["U0EXAMPLE001"]


def test_an_empty_batch_names_no_approver(roles):
    """`all()` of nothing is true; an empty batch must not reach everyone."""
    assert [p["slack_user_id"] for p in admins.notify_list_bundle([])] == ["U0EXAMPLE001"]


def test_the_old_model_is_unchanged(roles):
    roles["v2"] = False
    assert [p["slack_user_id"] for p in admins.notify_list_bundle(ITEMS)] == ["U0EXAMPLE001"]


def test_an_admin_who_is_also_an_approver_is_listed_once(roles):
    roles["approvers"] = [dict(ADMIN), CAPTAIN]
    roles["can"]["U0EXAMPLE001"] = {1, 2}
    got = [p["slack_user_id"] for p in admins.notify_list_bundle(ITEMS)]
    assert got.count("U0EXAMPLE001") == 1


def test_the_fan_out_asks_with_the_requester_on_every_item():
    """A captain's scope is their own team's requests, and `bundles.list_items`
    does not carry the requester. The scope row comes from one builder, which
    takes it from the bundle, for the fan-out and for each DM's buttons."""
    row = notifications._bundle_scope_item(
        {"requester_slack_id": "U0EXAMPLE009"},
        {"id": 7, "query": "select 1", "target_server_id": 3,
         "required_tier": "ro", "engine": "postgres"})
    assert row["requester_slack_id"] == "U0EXAMPLE009"
    assert {"required_tier", "engine", "target_server_id", "query"} <= set(row)
    src = inspect.getsource(notifications.notify_admins_bundle)
    assert "admins.notify_list_bundle(" in src and "_bundle_scope_item(bundle, it)" in src
    assert "_bundle_scope_item(bundle, it)" in inspect.getsource(notifications._build_bundle_dm_blocks)


# --- the bulk buttons admit the same people -----------------------------------


@pytest.fixture
def guard(monkeypatch):
    st = {"admin": False, "pending": [{"id": 1}, {"id": 2}], "can": {1, 2},
          "authority": True, "dms": [], "acked": 0}
    monkeypatch.setattr(handlers.admins, "is_admin", lambda uid: st["admin"])
    monkeypatch.setattr(handlers, "_bundle_pending_items", lambda bid: list(st["pending"]))
    monkeypatch.setattr(handlers.admins, "can_approve", lambda uid, r: r["id"] in st["can"])
    monkeypatch.setattr(handlers.admins, "has_approval_authority", lambda uid: st["authority"])
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda client, uid, text: st["dms"].append(text))
    monkeypatch.setattr(handlers.profile_sync, "maybe_backfill_user_profile", lambda c, u: None)
    return st


def _press(st):
    def ack():
        st["acked"] += 1
    return handlers._guard_bundle(ack, None, {"user": {"id": "U0EXAMPLE002"}}, 42)


def test_a_captain_who_can_clear_every_pending_item_may_press(guard):
    assert _press(guard) is True and guard["dms"] == []


def test_a_captain_missing_one_item_is_refused_with_the_scope_sentence(guard):
    guard["can"] = {1}
    assert _press(guard) is False
    assert guard["acked"] == 1 and "outside your approval scope" in guard["dms"][0]


def test_someone_with_no_approval_role_is_refused_as_before(guard):
    guard["can"], guard["authority"] = set(), False
    assert _press(guard) is False
    assert "not an authorized admin" in guard["dms"][0]


def test_an_admin_passes_without_a_scope_read(guard):
    guard["admin"], guard["can"] = True, set()
    assert _press(guard) is True


def test_a_batch_with_nothing_pending_is_not_a_captains_to_press(guard):
    guard["pending"] = []
    assert _press(guard) is False


def test_both_bulk_buttons_use_the_batch_guard():
    for fn in (handlers.handle_bundle_approve_all, handlers.handle_bundle_reject_all):
        src = inspect.getsource(fn)
        assert "_guard_bundle(" in src and "_guard_admin(" not in src, fn.__name__
