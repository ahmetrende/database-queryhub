"""A request that QueryHub approved by itself reads that way.

The Messages tab used to say "Submitted RO query for DBA approval", and then
"Approved by <the requester> (super-admin)". No DBA saw that query and nobody
approved it. The Audit log tab called the requester "you" on the approval row.
The history list named the requester as the approver.

Three paths approve at submit: a waiver (or the archive rule), the fingerprint
cache, and a super-admin's own access. The feed now says "Submitted RO query"
and "Auto-approved by QueryHub: <why>". Every surface names the system as the
approver. The stored decision does not change. A super-admin's own id stays on
the request row, so the audit trail still shows who had the authority.
"""
from __future__ import annotations

import inspect
import logging
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from queryhub import auto_approve, core_submit, executor
from queryhub.web import app as web_app
from queryhub.web import deps, mapping, routes_data, routes_queries

SUPER = "U0EXAMPLE001"
OTHER = "U0EXAMPLE009"
T0 = datetime(2026, 10, 21, 11, 0, 0, tzinfo=timezone.utc)


def _row(**over):
    """A completed read that a super-admin submitted, as the request row holds it."""
    row = {"id": 5, "query": "SELECT 1", "status": "completed", "database_name": "app",
           "requester_slack_id": SUPER, "target_server_id": 7, "engine": "postgres",
           "executed_tier": "ro", "executed_target_id": None, "run_on": None,
           "created_at": T0, "decided_at": T0, "executed_at": T0, "completed_at": T0,
           "row_count": 1, "scheduled_for": None, "error_message": None,
           "run_notes": None, "decided_by_slack_id": SUPER,
           "decided_by_name": "Jordan Ray (super-admin)"}
    row.update(over)
    return row


def _audit(action, details=None, actor=SUPER, name="Jordan Ray"):
    return {"actor_slack_id": actor, "actor_name": name, "action": action,
            "created_at": T0, "details": details if details is not None else {}}


def _texts(row, audits):
    return [m["text"] for m in mapping.status_messages(row, audits)]


# ---- the feed, one case per approval path ----------------------------------

def test_a_super_admins_own_query_is_approved_by_the_system():
    audits = [_audit("submitted", {"super_admin": True}),
              _audit("auto_approved_super", {"reason": "super-admin full access"})]
    msgs = mapping.status_messages(_row(), audits)
    assert [m["text"] for m in msgs][:2] == [
        "Submitted RO query as request #5.", "Auto-approved by QueryHub: super-admin access."]
    assert [m["kind"] for m in msgs][:2] == ["info", "ok"]
    everything = " ".join(m["text"] for m in msgs)
    assert "Jordan Ray" not in everything and "DBA approval" not in everything


def test_a_waiver_says_which_grant():
    row = _row(decided_by_slack_id="AUTO",
               decided_by_name="auto-approved (grant #12, max_tier=ro, no expiry)")
    audits = [_audit("auto_approved", {"grant_id": 12, "max_tier": "ro"},
                     actor="AUTO", name=None)]
    assert _texts(row, audits)[:2] == [
        "Submitted RO query as request #5.", "Auto-approved by QueryHub: auto-approve grant #12."]


def test_the_archive_rule_says_which_role():
    row = _row(decided_by_slack_id="AUTO", decided_by_name="auto-approved (archive: owner lead)")
    audits = [_audit("auto_approved", {"basis": "archive: owner lead", "max_tier": "ro"},
                     actor="AUTO", name=None)]
    assert _texts(row, audits)[1] == "Auto-approved by QueryHub: archive rule (owner lead)."


def test_the_fingerprint_cache_says_which_request():
    row = _row(decided_by_slack_id="AUTO", decided_by_name="auto-approved (fingerprint match)")
    audits = [_audit("auto_approved_fingerprint", {"fingerprint_match_request_id": 77},
                     actor="AUTO", name=None)]
    assert _texts(row, audits)[1] == "Auto-approved by QueryHub: same query as request #77."


def test_details_that_arrive_as_json_text_are_read_too():
    row = _row(decided_by_slack_id="AUTO", decided_by_name="auto")
    audits = [_audit("auto_approved", '{"grant_id": 3, "max_tier": "ro"}', actor="AUTO", name=None)]
    assert _texts(row, audits)[1] == "Auto-approved by QueryHub: auto-approve grant #3."


def test_without_audit_rows_the_request_row_still_says_it():
    assert _texts(_row(decided_by_slack_id="AUTO", decided_by_name="auto"), None)[:2] == [
        "Submitted RO query as request #5.", "Auto-approved by QueryHub."]
    assert _texts(_row(), None)[1] == "Auto-approved by QueryHub: super-admin access."


# ---- what must not change --------------------------------------------------

def test_a_person_approval_reads_as_before():
    row = _row(decided_by_slack_id=OTHER, decided_by_name="dba.x")
    assert _texts(row, [_audit("submitted")])[:2] == [
        "Submitted RO query as request #5 for DBA approval.", "Approved by dba.x."]


def test_a_person_who_approves_their_own_request_is_still_named():
    """decided_by equals the requester here too, but the by-hand path never
    writes the super-admin label, so this is not an automatic approval."""
    row = _row(decided_by_name="Jordan Ray")
    assert _texts(row, [_audit("submitted")])[:2] == [
        "Submitted RO query as request #5 for DBA approval.", "Approved by Jordan Ray."]


def test_a_request_nobody_has_decided_still_waits_for_a_dba():
    row = _row(status="pending", decided_at=None, decided_by_slack_id=None,
               decided_by_name=None, executed_at=None, completed_at=None)
    assert _texts(row, [_audit("submitted")]) == ["Submitted RO query as request #5 for DBA approval."]


def test_a_rejection_still_names_the_person():
    row = _row(status="rejected", decided_by_slack_id=OTHER, decided_by_name="dba.x",
               decision_reason="too broad", executed_at=None, completed_at=None)
    assert _texts(row, [_audit("submitted")])[-1] == "Rejected by dba.x: too broad"


# ---- the label, the audit tab and the history list -------------------------

def test_the_approver_of_a_super_admins_own_query_is_the_system():
    name = auto_approve.super_decided_name("Jordan Ray", SUPER)
    assert mapping.approver_label(SUPER, name, None, SUPER) == "auto-approve"
    # A caller without the requester cannot tell, and keeps the old answer.
    assert mapping.approver_label(SUPER, name) == name
    assert mapping.approver_label(SUPER, "Jordan Ray", None, SUPER) == "Jordan Ray"
    assert mapping.approver_label(OTHER, name, None, SUPER) == name


def test_the_audit_tab_does_not_call_the_requester_the_approver():
    rows = [_audit("submitted"), _audit("auto_approved_super", {"tier": "ro"}),
            _audit("auto_approved", actor="AUTO", name=None),
            _audit("execution_started", actor=None, name=None)]
    assert [e["actor"] for e in mapping.audit_entries(rows, SUPER)] == [
        "you", "auto-approve", "auto-approve", "system"]


def test_the_history_row_names_the_system_as_the_approver():
    entry = mapping.history_entry(_row(), lambda tid: "prod-main", None, None)
    assert entry["approver"] == "auto-approve"
    person = mapping.history_entry(_row(decided_by_slack_id=OTHER, decided_by_name="dba.x"),
                                   lambda tid: "prod-main", None, None)
    assert person["approver"] == "dba.x"


# ---- the first line names the request, at the stored tier ------------------

def test_the_first_line_names_the_request_and_its_stored_tier():
    """Request 12180 was a DDL script that opened with a DO block. The text
    reads as RO, so the line said "Submitted RO query" and gave no number."""
    row = _row(id=12180, query="DO $$ BEGIN PERFORM 1; END $$;", required_tier="ddl",
               decided_by_slack_id="AUTO", decided_by_name="auto")
    assert _texts(row, [_audit("auto_approved_super")])[0] == "Submitted DDL query as request #12180."
    assert mapping.request_tier(row) == "DDL"


def test_a_row_without_a_stored_tier_falls_back_to_the_text():
    assert mapping.request_tier(_row(required_tier=None, query="SELECT 1")) == "RO"
    assert mapping.request_tier(_row(required_tier="", query="DROP TABLE t")) == "DDL"


def test_a_row_without_an_id_still_reads():
    row = _row(decided_by_slack_id="AUTO", decided_by_name="auto")
    del row["id"]
    assert _texts(row, None)[0] == "Submitted RO query."


def test_the_waiting_line_names_the_request_too():
    row = _row(status="pending", decided_at=None, decided_by_slack_id=None,
               decided_by_name=None, executed_at=None, completed_at=None, required_tier="rw")
    assert _texts(row, [_audit("submitted")]) == ["Submitted RW query as request #5 for DBA approval."]


def test_the_history_row_carries_the_stored_tier():
    ddl = mapping.history_entry(_row(required_tier="ddl", query="DO $$ BEGIN END $$;"),
                                lambda tid: "prod-main", None, None)
    assert ddl["tier"] == "DDL"
    old = mapping.history_entry(_row(required_tier=None, query="SELECT 1"),
                                lambda tid: "prod-main", None, None)
    assert old["tier"] == "RO"


# ---- the reader finds what the writer writes -------------------------------

def test_the_reader_finds_what_the_writer_writes():
    assert auto_approve.super_decided_name("Jordan Ray", SUPER) == "Jordan Ray (super-admin)"
    assert auto_approve.super_decided_name(None, SUPER) == f"{SUPER} (super-admin)"
    for name in ("Jordan Ray", None):
        assert auto_approve.is_super_decision(
            SUPER, SUPER, auto_approve.super_decided_name(name, SUPER))
    assert not auto_approve.is_super_decision(None, None, "Jordan Ray (super-admin)")
    assert not auto_approve.is_super_decision(SUPER, OTHER, "Jordan Ray (super-admin)")


def test_both_writers_use_the_shared_super_admin_label():
    """The single submit and the batch submit write the same label. A copy of
    the old "(super-admin)" text in either would name the person again."""
    for module in (core_submit, routes_queries):
        source = inspect.getsource(module)
        assert "auto_approve.decided_by_name_for_super(" in source, module.__name__
        assert "(super-admin)" not in source, module.__name__


def test_the_new_label_names_the_system_and_the_person():
    assert auto_approve.decided_by_name_for_super("Jordan Ray", SUPER) == (
        "auto-approved (super-admin access, Jordan Ray)")
    assert auto_approve.decided_by_name_for_super(None, SUPER) == (
        f"auto-approved (super-admin access, {SUPER})")


def test_a_row_written_after_the_change_reads_as_the_system():
    label = auto_approve.decided_by_name_for_super("Jordan Ray", SUPER)
    row = _row(decided_by_slack_id="AUTO", decided_by_name=label)
    audits = [_audit("submitted", {"super_admin": True}),
              _audit("auto_approved_super", {"reason": "super-admin full access",
                                             "authority": SUPER}, actor="AUTO", name=None)]
    assert _texts(row, audits)[:2] == ["Submitted RO query as request #5.",
                                       "Auto-approved by QueryHub: super-admin access."]
    assert mapping.approver_label("AUTO", label, None, SUPER) == "auto-approve"
    assert [e["actor"] for e in mapping.audit_entries(audits, SUPER)] == ["you", "auto-approve"]


def test_slack_names_the_system_where_it_cannot_mention_a_person():
    assert executor._approved_by({"decided_by_slack_id": "AUTO"}) == "Auto-approved by QueryHub"
    assert executor._approved_by({"decided_by_slack_id": OTHER}) == f"Approved by <@{OTHER}>"
    assert executor._approved_by({"decided_by_slack_id": "auto:legacy",
                                  "decided_by_name": "old label"}) == "Approved by old label"
    assert executor._approved_by({}) == "Approved by an admin"


def test_no_status_line_mentions_the_stored_approver_directly():
    """A mention of the AUTO marker shows in Slack as a broken user."""
    assert "<@{request['decided_by_slack_id']}>" not in inspect.getsource(executor)


# ---- the HTTP surface ------------------------------------------------------

@pytest.fixture
def web(monkeypatch):
    logging.disable(logging.CRITICAL)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": SUPER, "provider": "slack", "sid": "s1", "name": "Jordan Ray"}
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: True)
    yield TestClient(app)
    logging.disable(logging.NOTSET)


def test_the_status_endpoint_tells_the_whole_story(web, monkeypatch):
    row = _row()
    audits = [_audit("submitted", {"super_admin": True}),
              _audit("auto_approved_super", {"tier": "ro"})]
    monkeypatch.setattr(routes_queries.db, "fetch_one", lambda sql, p=None: dict(row))
    monkeypatch.setattr(routes_queries.db, "fetch_all",
                        lambda sql, p=None: [dict(a) for a in audits])
    monkeypatch.setattr(routes_queries, "_alias_of", lambda tid: "prod-main")
    monkeypatch.setattr(routes_queries.people, "display_name", lambda *a, **k: "Jordan Ray")
    body = web.get("/api/queries/5").json()
    assert body["approver"] == "auto-approve"
    assert [m["text"] for m in body["messages"]][:2] == [
        "Submitted RO query as request #5.", "Auto-approved by QueryHub: super-admin access."]
    assert [e["actor"] for e in body["audit"]] == ["you", "auto-approve"]


def test_the_status_endpoint_hands_the_audit_rows_to_the_feed(web, monkeypatch):
    """Only the audit row knows the grant. Without it the feed would say
    "Auto-approved by QueryHub." and stop."""
    row = _row(decided_by_slack_id="AUTO", decided_by_name="auto")
    audits = [_audit("submitted"), _audit("auto_approved", {"grant_id": 12, "max_tier": "ro"},
                                          actor="AUTO", name=None)]
    monkeypatch.setattr(routes_queries.db, "fetch_one", lambda sql, p=None: dict(row))
    monkeypatch.setattr(routes_queries.db, "fetch_all",
                        lambda sql, p=None: [dict(a) for a in audits])
    monkeypatch.setattr(routes_queries, "_alias_of", lambda tid: "prod-main")
    monkeypatch.setattr(routes_queries.people, "display_name", lambda *a, **k: "x")
    body = web.get("/api/queries/5").json()
    assert body["messages"][1]["text"] == "Auto-approved by QueryHub: auto-approve grant #12."


def test_the_history_endpoint_reads_the_requester(web, monkeypatch):
    row = _row(executed_at=None)
    seen = []
    monkeypatch.setattr(routes_data.db, "fetch_all",
                        lambda sql, p=None: seen.append(" ".join(sql.split())) or [dict(row)])
    monkeypatch.setattr(routes_data, "_alias_of", lambda tid: "prod-main")
    monkeypatch.setattr(routes_data, "_conn_state_resolver", lambda uid: lambda tid: "ok")
    monkeypatch.setattr(routes_data.people, "namer", lambda names: lambda n: n)
    body = web.get("/api/history").json()
    assert body["history"][0]["approver"] == "auto-approve"
    assert "required_tier" in seen[0].split("FROM requests")[0], \
        "the tier chip needs the stored tier in the SELECT"
    assert "requester_slack_id" in seen[0].split("FROM requests")[0], \
        "the approver label needs the requester in the SELECT"
