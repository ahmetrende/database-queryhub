"""A request the bot hands to a DBA must always be closable, from Slack or the web.

The executor moves a DDL it is not allowed to run to awaiting_dba_manual and
puts Mark completed / Mark failed buttons on the admins' approval DM. An
auto-approved request -- a super-admin's, or one a waiver covered -- has no
approval DM, so the buttons went nowhere and the request stayed open for good,
while the web showed it as running. Four were found like that, the oldest five
weeks old. Closing now lives in one place, `manual_runs.close`, used by the
Slack buttons and the web panel alike.
"""
from types import SimpleNamespace

import pytest

from queryhub import executor, manual_runs
from queryhub import config as cfg
from queryhub.slack_app import handlers, notifications
from queryhub.web import mapping
from queryhub.web import routes_admin as ra
from queryhub.web import routes_queries as rq

ADMIN = "U0EXAMPLE001"
OTHER = "U0EXAMPLE002"


class Cur:
    def __init__(self, st): self.st = st
    def execute(self, sql, params=None): self.st["sql"].append((sql, params))
    def fetchone(self): return self.st["returned"]


class Txn:
    def __init__(self, st): self.st = st
    def __enter__(self): return Cur(self.st)
    def __exit__(self, *e): return False


@pytest.fixture
def store(monkeypatch):
    st = {"sql": [], "audit": [],
          "returned": {"id": 8, "status": "completed", "bundle_id": None,
                       "requester_slack_id": OTHER}}
    monkeypatch.setattr(manual_runs.db, "transaction", lambda: Txn(st))
    monkeypatch.setattr(manual_runs.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        st["audit"].append((rid, uid, action, details)))
    return st


# --- closing ------------------------------------------------------------------

def test_completing_records_who_ran_it(store):
    closed = manual_runs.close(8, completed=True, actor_id=ADMIN, actor_name="Ex")
    sql, params = store["sql"][0]
    assert "status = 'completed'" in sql and "status = 'awaiting_dba_manual'" in sql
    assert params[0] == f"manually completed by <@{ADMIN}>"
    assert store["audit"] == [(8, ADMIN, "completed_manually", None)]
    assert closed.completed and closed.row["id"] == 8


def test_failing_keeps_the_reason_for_the_requester(store):
    store["returned"] = {"id": 8, "status": "failed", "bundle_id": None}
    manual_runs.close(8, completed=False, actor_id=ADMIN, actor_name="Ex",
                      reason="  needs the owner  ")
    sql, params = store["sql"][0]
    assert "status = 'failed'" in sql
    assert params[0] == "manual DBA execution failed: needs the owner"
    assert store["audit"] == [(8, ADMIN, "failed_manually", {"reason": "needs the owner"})]


def test_a_request_closed_already_is_closed_once(store):
    """A double click, or Slack and the web racing, must not write twice."""
    store["returned"] = None
    assert manual_runs.close(8, completed=True, actor_id=ADMIN, actor_name="Ex") is None
    assert store["audit"] == []


def test_a_web_only_account_is_named_not_mentioned():
    assert manual_runs.actor_ref(ADMIN, "Ex") == f"<@{ADMIN}>"
    # `<@local:ex>` would print as literal text in Slack.
    assert manual_runs.actor_ref("local:ex", "Ex Ample") == "Ex Ample"


def test_the_list_is_what_the_buttons_would_admit(monkeypatch):
    rows = [{"id": 1, "target_server_id": 5}, {"id": 2, "target_server_id": 6}]
    monkeypatch.setattr(manual_runs.db, "fetch_all", lambda sql, *a: rows)
    monkeypatch.setattr(manual_runs.admins, "can_approve",
                        lambda uid, r: r["target_server_id"] == 5)
    assert [r["id"] for r in manual_runs.list_open_for(ADMIN)] == [1]


# --- telling people -----------------------------------------------------------

@pytest.fixture
def slack(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "update_bundle_admin_dms",
                        lambda c, b: calls.append(("bundle", b)))
    monkeypatch.setattr(notifications, "update_all_admin_messages",
                        lambda c, r, line, **k: calls.append(("cards", r["id"], k)))
    monkeypatch.setattr(notifications, "dm_requester",
                        lambda c, uid, text: calls.append(("requester", uid)))
    monkeypatch.setattr(notifications, "request_context_md", lambda r: "")
    monkeypatch.setattr(notifications, "request_context_with_query_md", lambda r: "")
    from queryhub import ratings
    monkeypatch.setattr(ratings, "maybe_prompt", lambda c, r: None)
    return calls


def test_closing_collapses_the_cards_and_tells_the_requester(slack):
    row = {"id": 8, "bundle_id": None, "requester_slack_id": OTHER}
    manual_runs.notify_closed(object(), manual_runs.Closed(row, True, None, "<@x>"))
    assert slack == [("cards", 8, {}), ("requester", OTHER)]


def test_a_bundle_item_updates_both_card_kinds_and_leaves_the_requester_to_the_summary(slack):
    row = {"id": 8, "bundle_id": 3, "requester_slack_id": OTHER}
    manual_runs.notify_closed(object(), manual_runs.Closed(row, False, "no", "<@x>"))
    assert slack == [("bundle", 3), ("cards", 8, {})]


def test_no_slack_client_means_no_slack_side_effects(slack):
    manual_runs.notify_closed(None, manual_runs.Closed({"id": 8}, True, None, "x"))
    assert slack == []


def test_both_slack_buttons_close_through_the_shared_path(monkeypatch):
    seen = []
    monkeypatch.setattr(handlers, "_guard_admin", lambda *a, **k: True)
    monkeypatch.setattr(handlers.manual_runs, "close",
                        lambda rid, **k: seen.append((rid, k["completed"], k.get("reason"))) or None)
    handlers.handle_dba_mark_completed(
        lambda *a, **k: None, {"actions": [{"value": "8"}], "user": {"id": ADMIN}}, None)
    body = {"view": {"private_metadata": "9", "state": {"values": {
        "reason_block": {"reason_input": {"value": "owner only"}}}}},
        "user": {"id": ADMIN}}
    handlers.handle_dba_failed_submission(lambda *a, **k: None, body, None)
    assert seen == [(8, True, None), (9, False, "owner only")]


# --- the escalation reaches someone -------------------------------------------

@pytest.fixture
def escalate(monkeypatch):
    st = {"calls": [], "has_dms": False}
    monkeypatch.setattr(executor.db, "transaction", lambda: Txn({"sql": [], "returned": None}))
    monkeypatch.setattr(executor.audit, "log_in", lambda *a, **k: None)
    n = executor.notifications
    monkeypatch.setattr(n, "update_all_admin_messages",
                        lambda c, r, line, **k: st["calls"].append(("update", k.get("dba_manual"))))
    monkeypatch.setattr(n, "update_bundle_admin_dms",
                        lambda c, b: st["calls"].append(("bundle", b)))
    monkeypatch.setattr(n, "has_admin_dms", lambda r: st["has_dms"])
    monkeypatch.setattr(n, "post_dba_manual_dms",
                        lambda c, r, line: st["calls"].append(("post", line)) or 1)
    monkeypatch.setattr(n, "dm_requester", lambda c, uid, text: st["calls"].append(("requester", uid)))
    monkeypatch.setattr(n, "request_context_md", lambda r: "")
    monkeypatch.setattr(executor, "_deliver_result_to_requester", lambda r: True)
    return st


def _req(**kw):
    r = {"id": 8930, "requester_slack_id": OTHER, "decided_by_slack_id": ADMIN,
         "decided_by_name": "Ex (super-admin)", "decided_at": None, "bundle_id": None}
    r.update(kw)
    return r


def test_an_auto_approved_escalation_posts_the_buttons_itself(escalate):
    executor._escalate_to_dba(object(), _req(), "Permission denied to create role")
    kinds = [c[0] for c in escalate["calls"]]
    assert kinds == ["update", "post", "requester"]
    assert "Permission denied to create role" in escalate["calls"][1][1]


def test_an_approved_escalation_reuses_the_approval_dm(escalate):
    escalate["has_dms"] = True
    executor._escalate_to_dba(object(), _req(), "must be owner of table t")
    assert [c[0] for c in escalate["calls"]] == ["update", "requester"]


def test_an_auto_approved_bundle_item_gets_buttons_too(escalate):
    executor._escalate_to_dba(object(), _req(bundle_id=3), "must be owner")
    assert [c[0] for c in escalate["calls"]] == ["bundle", "post"]


def test_a_waiver_approval_is_named_by_its_label_not_a_broken_mention(escalate):
    executor._escalate_to_dba(
        object(), _req(decided_by_slack_id=None, decided_by_name="auto-approved (grant #32)"),
        "must be owner")
    line = escalate["calls"][1][1]
    assert "auto-approved (grant #32)" in line and "<@" not in line


@pytest.fixture
def poster(monkeypatch):
    st = {"posted": [], "rows": [], "snippets": []}
    monkeypatch.setattr(cfg, "ENV", SimpleNamespace(slack_enabled=True))
    monkeypatch.setattr(notifications.targets, "get", lambda tid: SimpleNamespace(alias="ledger"))
    monkeypatch.setattr(notifications, "_dba_manual_blocks", lambda r, t, line: [{"line": line}])
    monkeypatch.setattr(notifications, "_display_overrides", lambda: {})
    monkeypatch.setattr(notifications.admins, "list_active",
                        lambda: [{"slack_user_id": ADMIN}, {"slack_user_id": OTHER}])
    monkeypatch.setattr(notifications.admins, "can_approve", lambda uid, r: uid == ADMIN)
    monkeypatch.setattr(notifications, "_post",
                        lambda c, **k: st["posted"].append(k["channel"]) or {"ts": "1.2"})
    monkeypatch.setattr(notifications.db, "execute",
                        lambda sql, params: st["rows"].append(params))
    monkeypatch.setattr(notifications, "_upload_query_snippet",
                        lambda c, ch, rid, q, ts: st["snippets"].append(rid))
    client = SimpleNamespace(conversations_open=lambda users: {"channel": {"id": "D-" + users}})
    return st, client


def test_the_card_goes_to_admins_who_could_close_it_and_is_recorded(poster):
    st, client = poster
    n = notifications.post_dba_manual_dms(
        client, {"id": 8930, "target_server_id": 1, "query": "x" * 600}, "line")
    assert n == 1 and st["posted"] == ["D-" + ADMIN]
    assert st["rows"] == [(8930, ADMIN, "D-" + ADMIN, "1.2")]
    # The DBA runs it by hand, so a long script arrives in full.
    assert st["snippets"] == [8930]


def test_no_card_without_slack(poster, monkeypatch):
    st, client = poster
    monkeypatch.setattr(cfg, "ENV", SimpleNamespace(slack_enabled=False))
    assert notifications.post_dba_manual_dms(client, {"id": 1, "target_server_id": 1}, "l") == 0
    assert st["posted"] == []


# --- the web ------------------------------------------------------------------

def test_the_web_stops_calling_it_running():
    assert mapping.status_to_web("awaiting_dba_manual") == "failed"
    assert mapping.awaiting_dba("awaiting_dba_manual") is True
    assert mapping.awaiting_dba("failed") is False
    assert "awaiting_dba_manual" in rq._WS_TERMINAL


def test_the_messages_tab_says_a_dba_takes_it_from_here():
    row = {"query": "CREATE ROLE r", "status": "awaiting_dba_manual",
           "created_at": None, "decided_at": None, "executed_at": None,
           "error_message": "requires DBA manual execution — Permission denied to create role"}
    texts = [m["text"] for m in mapping.status_messages(row)]
    assert "QueryHub could not run this: Permission denied to create role." in texts
    assert any("A DBA has to run it by hand" in t for t in texts)


@pytest.fixture
def web(monkeypatch):
    st = {"closed": None, "notified": []}
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, area, request=None: claims["sub"])
    monkeypatch.setattr(ra.db, "fetch_one", lambda sql, params: {"id": params[0]})
    monkeypatch.setattr(ra.manual_runs, "close", lambda rid, **k: st["closed"])
    monkeypatch.setattr(ra.manual_runs, "notify_closed", lambda c, closed: st["notified"].append(closed))
    monkeypatch.setattr(ra, "_slack_client", lambda: None)
    return st


CLAIMS = {"sub": ADMIN, "name": "Ex"}


def test_failing_from_the_web_needs_a_reason(web):
    with pytest.raises(Exception) as e:
        ra.admin_close_manual_run(8, ra.ManualCloseIn(completed=False, reason=" "), CLAIMS)
    assert e.value.status_code == 400


def test_closing_a_closed_request_from_the_web_is_a_conflict(web):
    with pytest.raises(Exception) as e:
        ra.admin_close_manual_run(8, ra.ManualCloseIn(completed=True), CLAIMS)
    assert e.value.status_code == 409


def test_closing_from_the_web_reports_the_new_status(web):
    web["closed"] = manual_runs.Closed({"id": 8, "status": "completed"}, True, None, "x")
    out = ra.admin_close_manual_run(8, ra.ManualCloseIn(completed=True), CLAIMS)
    assert out == {"id": "8", "status": "done"} and len(web["notified"]) == 1


def test_the_web_list_carries_what_a_dba_needs_to_run_it(monkeypatch, web):
    monkeypatch.setattr(ra.manual_runs, "list_open_for", lambda uid: [{
        "id": 8930, "requester_slack_id": OTHER, "requester_name": "Req",
        "target_server_id": 1, "target_alias": "ledger", "database_name": "main",
        "required_tier": "ddl", "query": "CREATE ROLE r", "created_at": None,
        "executed_at": None, "bundle_id": None, "justification": "new service account",
        "error_message": "requires DBA manual execution — Permission denied to create role"}])
    item = ra.admin_manual_runs(CLAIMS)["items"][0]
    assert item["connectionId"] == "ledger" and item["tier"] == "DDL"
    assert item["sql"] == "CREATE ROLE r"
    # The database's words and the requester's are two different reasons.
    assert item["refusal"] == "Permission denied to create role"
    assert item["reason"] == "new service account"
