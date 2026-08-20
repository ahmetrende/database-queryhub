"""Pure tests for web/mapping.py — the contract-critical conversions."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from queryhub.web import mapping


@pytest.fixture(autouse=True)
def _utc_display(monkeypatch):
    """Pin the display timezone to UTC so these assertions are independent
    of the live bot_config `web_display_timezone`."""
    monkeypatch.setattr(mapping, "_display_tz", lambda: ZoneInfo("UTC"))


def test_fmt_ts_ms_format():
    dt = datetime(2026, 7, 24, 3, 32, 8, 683423, tzinfo=timezone.utc)
    assert mapping.fmt_ts_ms(dt) == "2026-07-24 03:32:08.683"  # yyyy-MM-dd HH:mm:ss.fff
    assert mapping.fmt_time(dt) == "03:32:08"
    assert mapping.fmt_ts_ms(None) == ""
    assert mapping.fmt_time(None) == ""


def test_status_mapping_exact_strings():
    assert mapping.status_to_web("pending") == "pending"
    assert mapping.status_to_web("approved") == "approved"
    assert mapping.status_to_web("scheduled") == "approved"
    assert mapping.status_to_web("executing") == "running"
    assert mapping.status_to_web("awaiting_dba_manual") == "running"
    assert mapping.status_to_web("completed") == "done"
    assert mapping.status_to_web("failed") == "failed"
    assert mapping.status_to_web("rejected") == "rejected"
    assert mapping.status_to_web("changes_requested") == "rejected"
    assert mapping.status_to_web("cancelled") == "rejected"
    assert mapping.status_to_web("weird_future_status") == "pending"


def test_env_heuristic():
    assert mapping.env_of("alpha-prod-payments") == "production"
    assert mapping.env_of("beta-prod") == "production"
    assert mapping.env_of("gamma-test") == "staging"
    assert mapping.env_of("delta-staging") == "staging"


def test_engine_label():
    assert mapping.engine_label("postgres") == "PostgreSQL"
    assert mapping.engine_label(None) == "PostgreSQL"
    assert mapping.engine_label("clickhouse") == "ClickHouse"


def test_approver_label_auto_sentinel():
    assert mapping.approver_label("AUTO", "auto-approve grant #5") == "auto-approve"
    assert mapping.approver_label("U123", "dba.admin") == "dba.admin"
    assert mapping.approver_label(None, None) is None


def test_history_entry_shape():
    row = {
        "id": 1328,
        "query": "SELECT count(*) FROM requests",
        "target_server_id": 1,
        "database_name": "queryhub",
        "status": "completed",
        "row_count": 1,
        "created_at": datetime(2026, 7, 14, 12, 53, tzinfo=timezone.utc),
        "decided_by_slack_id": "U0ADMIN0001",
        "decided_by_name": "alex.kim",
    }
    e = mapping.history_entry(row, lambda tid: "alpha-admin")
    assert e == {
        "id": "1328",
        "sql": "SELECT count(*) FROM requests",
        "connectionId": "alpha-admin",
        "databaseId": "queryhub",
        "tier": "RO",
        "status": "done",
        "rowCount": 1,
        "createdAt": "2026-07-14T12:53:00+00:00",
        "approver": "alex.kim",
    }


def test_saved_entry_label_fallback():
    row = {"id": 7, "query": "SELECT 1", "target_server_id": None,
           "database_name": None, "label": None}
    e = mapping.saved_entry(row, lambda tid: None)
    assert e["id"] == "7" and e["name"] == "SELECT 1"
    assert e["connectionId"] is None


def test_classification_multi_statement():
    # The only legal multi-statement shape: SET LOCAL prelude + ONE main
    # statement (anything else is blocked upstream by query_safety).
    c = mapping.classification_of("SET LOCAL work_mem='64MB'; SELECT 1")
    assert c["tier"] == "RO"
    assert c["multi"] is True
    assert [s["kw"] for s in c["statements"]] == ["SET", "SELECT"]


def test_classification_rw_single():
    c = mapping.classification_of("DELETE FROM t WHERE false")
    assert c["tier"] == "RW"
    assert c["statements"] == [{"kw": "DELETE", "tier": "RW"}]


def test_classification_single_ro():
    c = mapping.classification_of("SELECT id FROM users")
    assert c == {"tier": "RO", "multi": False,
                 "statements": [{"kw": "SELECT", "tier": "RO"}]}


def test_pii_preview_star_and_columns(monkeypatch):
    from queryhub import pii
    monkeypatch.setattr(pii, "column_pii_map",
                        lambda cols: {i: "email" for i, c in enumerate(cols)
                                      if c.lower() == "email"})
    p = mapping.pii_preview("SELECT email, id FROM users")
    assert p["star"] is False
    assert p["columns"] == [{"col": "email", "label": "Email address",
                             "mask": "partial"}]
    p2 = mapping.pii_preview("SELECT * FROM users")
    assert p2["star"] is True


def test_status_messages_completed_flow():
    row = {
        "query": "SELECT 1",
        "status": "completed",
        "created_at": datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone.utc),
        "decided_at": datetime(2026, 7, 14, 10, 1, 0, tzinfo=timezone.utc),
        "decided_by_slack_id": "U1", "decided_by_name": "dba.x",
        "executed_at": datetime(2026, 7, 14, 10, 1, 1, tzinfo=timezone.utc),
        "completed_at": datetime(2026, 7, 14, 10, 1, 2, tzinfo=timezone.utc),
        "row_count": 12,
    }
    msgs = mapping.status_messages(row)
    kinds = [m["kind"] for m in msgs]
    texts = [m["text"] for m in msgs]
    assert kinds == ["info", "ok", "info", "ok"]
    assert "Approved by dba.x." in texts
    # The web message feed is channel-neutral — it must not assume Slack.
    assert not any("Slack" in t for t in texts)
    # Times are the full yyyy-MM-dd HH:mm:ss.fff (display tz), rendered verbatim
    # by the Messages tab — same format as the audit feed.
    import re as _re
    assert all(_re.match(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}$', m["time"]) for m in msgs)


def test_status_messages_rejected():
    row = {"query": "DELETE FROM t", "status": "rejected",
           "created_at": datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
           "decided_at": datetime(2026, 7, 14, 10, 2, tzinfo=timezone.utc),
           "decided_by_slack_id": "U1", "decided_by_name": "dba.x",
           "decision_reason": "too broad"}
    msgs = mapping.status_messages(row)
    assert msgs[-1]["kind"] == "err" and "too broad" in msgs[-1]["text"]


def test_status_messages_scheduled_line():
    row = {"query": "SELECT 1", "status": "scheduled",
           "created_at": datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
           "scheduled_for": datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc),
           "decided_at": datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
           "decided_by_slack_id": "AUTO", "decided_by_name": "auto"}
    texts = [m["text"] for m in mapping.status_messages(row)]
    assert any("Scheduled for 2026-07-15 02:00" in t for t in texts)
    assert not any("Slack" in t for t in texts)


def test_status_messages_no_scheduled_line_after_run():
    row = {"query": "SELECT 1", "status": "completed",
           "created_at": datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
           "scheduled_for": datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
           "executed_at": datetime(2026, 7, 14, 10, 0, 1, tzinfo=timezone.utc),
           "completed_at": datetime(2026, 7, 14, 10, 0, 2, tzinfo=timezone.utc),
           "row_count": 1}
    texts = [m["text"] for m in mapping.status_messages(row)]
    assert not any("Scheduled for" in t for t in texts)  # already ran


def test_run_ms():
    row = {"executed_at": datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone.utc),
           "completed_at": datetime(2026, 7, 14, 10, 0, 1, 500000, tzinfo=timezone.utc)}
    assert mapping.run_ms(row) == 1500
    assert mapping.run_ms({}) is None


def test_parse_index_unique_and_cols():
    i = mapping.parse_index({"name": "access_codes_pkey",
        "def": "CREATE UNIQUE INDEX access_codes_pkey ON s.access_codes USING btree (id)"})
    assert i == {"name": "access_codes_pkey", "cols": ["id"], "unique": True, "pk": True}
    i2 = mapping.parse_index({"name": "idx_ac_session",
        "def": "CREATE INDEX idx_ac_session ON s.access_codes USING btree (session_id, code)"})
    assert i2["cols"] == ["session_id", "code"] and i2["unique"] is False and i2["pk"] is False


def test_fk_map():
    fks = [{"name": "x", "def": "FOREIGN KEY (session_id) REFERENCES event_mgmt.sessions(id) ON DELETE CASCADE"}]
    assert mapping.fk_map(fks) == {"session_id": "sessions.id"}
    assert mapping.fk_map(None) == {}


def test_web_text_strips_slack_mrkdwn():
    # the exact leak from the screenshot
    assert mapping.web_text(":bar_chart: Result · ~1 rows (~4 B) · size `XS` (cost 0)") \
        == "Result · ~1 rows (~4 B) · size XS (cost 0)"
    assert mapping.web_text("<https://x.io|the link> after") == "the link after"
    assert mapping.web_text(":warning: :fire: hot") == "hot"
    assert mapping.web_text(None) is None
    assert mapping.web_text("") == ""


def test_initials():
    assert mapping._initials("Jordan Ray") == "JR"
    assert mapping._initials("alex.kim") == "AK"     # dot-separated handle
    assert mapping._initials("madonna") == "MA"
    assert mapping._initials("") == "?"
    assert mapping._initials(None) == "?"


def test_queue_item_ddl_escalates(monkeypatch):
    from queryhub import pii
    monkeypatch.setattr(pii, "column_pii_map", lambda cols: {})
    row = {
        "id": 8421,
        "requester_slack_id": "U07EF",
        "requester_name": "Jordan Ray",
        "target_server_id": 3,
        "database_name": "payments",
        "query": "ALTER TABLE payouts ADD COLUMN note text",
        "justification": "Add a note column.",
        "risk_summary": None,
        "created_at": datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
    }
    item = mapping.queue_item(row, lambda tid: "prod-main")
    assert item["id"] == "8421"
    assert item["tier"] == "DDL"
    assert item["escalate"] is True
    assert item["connectionId"] == "prod-main"
    assert item["databaseId"] == "payments"
    assert item["reason"] == "Add a note column."
    assert item["submitter"] == {"name": "Jordan Ray", "initials": "JR",
                                  "slackId": "U07EF", "trust": None}
    assert item["statements"] == 1
    assert item["submittedAt"] == "2026-07-14T12:00:00+00:00"


def test_queue_item_ro_no_escalate_and_piicols(monkeypatch):
    from queryhub import pii
    monkeypatch.setattr(pii, "column_pii_map",
                        lambda cols: {i: "email" for i, c in enumerate(cols)
                                      if c.lower() == "email"})
    row = {"id": 5, "requester_slack_id": "U1", "requester_name": None,
           "target_server_id": None, "database_name": "app",
           "query": "SELECT email FROM users", "justification": None,
           "risk_summary": "seq scan on users",
           "created_at": datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)}
    item = mapping.queue_item(row, lambda tid: None)
    assert item["tier"] == "RO" and item["escalate"] is False
    assert item["connectionId"] is None            # no target → no alias
    assert item["submitter"]["name"] == "U1"       # falls back to slack id
    assert item["submitter"]["initials"] == "?"
    assert item["piiCols"] == [{"col": "email", "label": "Email address",
                                "mask": "partial"}]
    assert item["riskSummary"] == "seq scan on users"


def test_grant_entry_user_all_dbs():
    row = {"_gid": "u:U1:3", "_subject_type": "user", "subject": "U1",
           "subject_name": "Ada", "target_server_id": 3,
           "allowed_databases": None, "mode": "rw",
           "granted_by": "U0ADMIN", "granted_at": None}
    e = mapping.grant_entry(row, lambda tid: "prod-main")
    assert e["id"] == "u:U1:3" and e["subjectType"] == "user"
    assert e["connectionId"] == "prod-main" and e["tier"] == "RW"
    assert e["databases"] == "*"        # NULL allowed_databases = all
    assert e["expiresAt"] is None


def test_grant_entry_team_specific_dbs():
    row = {"_gid": "t:7:3", "_subject_type": "team", "subject": "7",
           "subject_name": "payments", "target_server_id": 3,
           "allowed_databases": ["b", "a"], "mode": "ro", "granted_by": None}
    e = mapping.grant_entry(row, lambda tid: "prod-main")
    assert e["subjectType"] == "team" and e["databases"] == ["a", "b"]
    assert e["tier"] == "RO"


def test_auto_grant_entry():
    from datetime import datetime, timezone
    row = {"id": 25, "slack_user_id": "U9", "max_tier": "ro",
           "target_server_id": 52, "database_name": "catalog", "reason": "pilot",
           "expires_at": None, "granted_by": "U0ADMIN",
           "granted_at": datetime(2026, 7, 14, tzinfo=timezone.utc)}
    e = mapping.auto_grant_entry(row, lambda tid: "beta-cache")
    # No name columns on this row, so both name fields fall back to the id —
    # the grid still shows something identifying rather than an empty cell.
    assert e == {"id": "25", "user": "U9", "userName": "U9", "tier": "RO",
                 "connectionId": "beta-cache", "databaseId": "catalog",
                 "maxRows": None, "reason": "pilot", "expiresAt": None,
                 "createdBy": "U0ADMIN", "createdByName": "U0ADMIN",
                 "grantedAt": "2026-07-14T00:00:00+00:00"}


def test_auto_grant_entry_prefers_the_resolved_names():
    """The subject column showed a raw Slack id, which tells a reviewer
    nothing about whose access they are looking at."""
    row = {"id": 48, "slack_user_id": "U0AB12CD34", "max_tier": "ro",
           "target_server_id": 21, "database_name": None, "reason": None,
           "expires_at": None, "granted_by": "U0ADMIN123",
           "granted_at": None,
           "user_name": "Jordan Ray", "granted_by_name": "Sam Ellis"}
    e = mapping.auto_grant_entry(row, lambda tid: "prod-main")
    assert e["user"] == "U0AB12CD34"          # the identity is still the id
    assert e["userName"] == "Jordan Ray"
    assert e["createdByName"] == "Sam Ellis"
    assert e["databaseId"] is None             # NULL = every database


def test_endpoint_request_entry():
    row = {"id": 31, "requester_slack_id": "U2", "requester_name": "Lin",
           "target_server_id": 3, "database_name": "payments",
           "reason": "need read", "status": "pending", "created_at": None}
    e = mapping.endpoint_request_entry(row, lambda tid: "prod-main")
    assert e["id"] == "er_31" and e["requester"] == "Lin"
    assert e["server"] == "prod-main" and e["status"] == "pending"


# ---------------------------------------------------------------------------
# The request id in the audit log. Asked for while looking at the page: every
# query decision belongs to a request, the requester now sees that number in
# their tab from the moment it opens, and the log showed no way to connect the
# two — its `id` field is the audit ROW's id, which nobody outside the table
# has ever seen.
# ---------------------------------------------------------------------------


def _audit_row(**over):
    row = {"id": 8459, "request_id": 2019, "action": "completed",
           "actor_slack_id": "U1", "actor_name": "Ada", "details": {},
           "created_at": None,
           "req_target_server_id": 21, "req_database_name": "notify_service",
           "req_requester_name": "Ada", "req_query": "select * from t",
           "req_row_count": 5000, "req_executed_at": None,
           "req_completed_at": None}
    row.update(over)
    return row


def test_the_audit_entry_carries_the_request_id():
    e = mapping.admin_audit_entry(_audit_row(), "other", lambda t: "alias", lambda x: x)
    assert e["requestId"] == "2019"
    # and it is NOT the audit row's own id, which is the mistake to guard
    assert e["id"] == "8459"


def test_an_entry_with_no_request_behind_it_has_no_id():
    """Grants, admin scopes, auto-approve windows and the kill switch are real
    audit events with no request — the column shows a dash, not "None"."""
    e = mapping.admin_audit_entry(
        _audit_row(request_id=None, action="grant_added",
                   req_target_server_id=None, req_database_name=None,
                   req_requester_name=None, req_query=None),
        "grant", lambda t: "alias", lambda x: x)
    assert e["requestId"] is None


def test_the_id_is_a_string_like_every_other_id_in_the_contract():
    """API_CONTRACT ids are strings; a number here would be the one field the
    frontend has to special-case."""
    e = mapping.admin_audit_entry(_audit_row(request_id=7), "other",
                                  lambda t: "a", lambda x: x)
    assert e["requestId"] == "7" and isinstance(e["requestId"], str)


def test_the_table_renders_the_column():
    """Header and cell counts have to match or the CSS grid shifts every row —
    which is exactly what a 9-track grid with 10 children does."""
    import re
    from pathlib import Path
    web = Path(__file__).resolve().parent.parent / "QueryHubWeb"
    jsx = (web / "qh-admin-insights.jsx").read_text(encoding="utf-8")
    assert "qh-auditreq" in jsx
    assert "a.requestId" in jsx
    css = (web / "QueryHub.html").read_text(encoding="utf-8")
    m = re.search(r"\.qh-auditline \{[^}]*grid-template-columns:([^;]+);", css)
    assert m, "the audit grid definition moved"
    tracks = m.group(1).replace("minmax(0, 1.3fr)", "X").replace(
        "minmax(0, 2fr)", "X").split()
    assert len(tracks) == 10, (
        f"the audit row has 10 cells but the grid declares {len(tracks)} tracks: "
        f"{m.group(1).strip()}")


def test_queue_item_carries_its_batch_position(monkeypatch):
    """Items of one `/sql batch` are one piece of work to the approver, but
    arrive as N rows. Without these the queue cannot group them."""
    from queryhub import pii
    monkeypatch.setattr(pii, "column_pii_map", lambda cols: {})
    row = {"id": 4370, "requester_slack_id": "U9", "target_server_id": 1,
           "database_name": "d", "query": "SELECT 1", "status": "pending",
           "bundle_id": 58, "position": 1, "bundle_size": 2,
           "required_tier": "ro", "origin": "slack"}
    e = mapping.queue_item(row, lambda tid: "conn")
    assert (e["bundleId"], e["bundlePosition"], e["bundleSize"]) == (58, 1, 2)


def test_a_standalone_request_has_no_batch_fields(monkeypatch):
    from queryhub import pii
    monkeypatch.setattr(pii, "column_pii_map", lambda cols: {})
    row = {"id": 4371, "requester_slack_id": "U9", "target_server_id": 1,
           "database_name": "d", "query": "SELECT 1", "status": "pending",
           "bundle_id": None, "position": None, "bundle_size": None,
           "required_tier": "ro", "origin": "web"}
    e = mapping.queue_item(row, lambda tid: "conn")
    assert e["bundleId"] is None
    assert e["bundlePosition"] is None and e["bundleSize"] is None
