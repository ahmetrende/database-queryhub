"""plan_authoritative_disables — the inventory-says-gone sweep planner.

Rule A: host matches a v_server row with is_deleted=true → disable.
Rule B: host unknown to v_server BUT its identifier (first dotted
        segment) is alive at a different endpoint → disable.
Plain absence (collector blind spot) must stay untouched.
"""
import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "import_targets_from_inventory",
    Path(__file__).resolve().parent.parent
    / "scripts" / "import_targets_from_inventory.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["import_targets_from_inventory"] = _mod
_spec.loader.exec_module(_mod)

plan = _mod.plan_authoritative_disables


def _srv(ident, endpoint, deleted=False, deleted_at=None):
    return {"db_instance_identifier": ident, "endpoint": endpoint,
            "is_deleted": deleted, "deleted_at": deleted_at}


def _tgt(tid, alias, host):
    return {"id": tid, "alias": alias, "host": host}


def test_rule_a_soft_deleted_endpoint_is_disabled():
    servers = [_srv("svc-a", "svc-a.x.example.com", deleted=True,
                    deleted_at="2026-04-27")]
    targets = [_tgt(1, "svc-a", "svc-a.x.example.com")]
    plans = plan(servers, targets)
    assert len(plans) == 1
    assert plans[0]["id"] == 1
    assert "deleted" in plans[0]["reason"]


def test_rule_b_identifier_reused_at_new_endpoint():
    # The old PG endpoint vanished from v_server; the identifier now
    # lives at a ClickHouse Cloud endpoint. The stale target must go.
    servers = [_srv("svc-b", "abc123.region.aws.clickhouse.cloud")]
    targets = [_tgt(2, "svc-b", "svc-b.x.rds.example.com")]
    plans = plan(servers, targets)
    assert len(plans) == 1
    assert plans[0]["id"] == 2
    assert "different endpoint" in plans[0]["reason"]
    assert "clickhouse.cloud" in plans[0]["detail"]


def test_plain_absence_is_left_alone():
    # Host missing from v_server and identifier unknown → collector
    # blind spot; hands off.
    servers = [_srv("other", "other.x.example.com")]
    targets = [_tgt(3, "outside", "outside.y.example.com")]
    assert plan(servers, targets) == []


def test_alive_endpoint_untouched():
    servers = [_srv("svc-c", "svc-c.x.example.com")]
    targets = [_tgt(4, "svc-c", "svc-c.x.example.com")]
    assert plan(servers, targets) == []


def test_rule_b_requires_live_replacement():
    # Identifier exists only as a DELETED row elsewhere → that is not a
    # live replacement; plain absence rules apply (hands off).
    servers = [_srv("svc-d", "svc-d-new.x.example.com", deleted=True)]
    targets = [_tgt(5, "svc-d", "svc-d.x.example.com")]
    assert plan(servers, targets) == []


def test_rule_b_same_endpoint_not_a_replacement():
    # Identifier's live row IS this endpoint (normal case) — covered by
    # the by-endpoint branch, never a replacement.
    servers = [_srv("svc-e", "svc-e.x.example.com")]
    targets = [_tgt(6, "svc-e", "svc-e.x.example.com")]
    assert plan(servers, targets) == []


def test_null_endpoint_rows_ignored():
    servers = [_srv("svc-f", None, deleted=True), _srv("svc-f", "")]
    targets = [_tgt(7, "svc-f", "svc-f.x.example.com")]
    assert plan(servers, targets) == []


# ---------------------------------------------------------------------------
# One endpoint, two v_server rows: an instance deleted and recreated
# ---------------------------------------------------------------------------

def test_a_live_row_wins_over_a_deleted_row_for_the_same_endpoint():
    """v_server keeps the deleted instance's row when the same identifier is
    recreated, and the endpoint comes back identical. The index used to keep
    whichever row came LAST, so a live target was one row order away from being
    disabled as deleted. Both orders must leave it alone."""
    ep = "mail.internal.example.com"
    old = _srv("mail", ep, deleted=True, deleted_at="2026-08-01")
    new = _srv("mail", ep)
    targets = [_tgt(9, "mail", ep)]
    assert plan([old, new], targets) == []
    assert plan([new, old], targets) == []


# ---------------------------------------------------------------------------
# plan_deletion_marks — target_servers.deleted_at (migration 139)
# ---------------------------------------------------------------------------

marks_for = _mod.plan_deletion_marks


def _t(tid, alias, host, deleted_at=None):
    return {"id": tid, "alias": alias, "host": host, "deleted_at": deleted_at}


def test_a_reported_deletion_is_marked_with_the_inventorys_own_date():
    servers = [_srv("svc-a", "svc-a.x.example.com", deleted=True,
                    deleted_at="2026-07-14 09:30:00+00")]
    marks, clears = marks_for(servers, [_t(1, "svc-a", "svc-a.x.example.com")])
    assert [m["id"] for m in marks] == [1]
    assert marks[0]["deleted_at"] == "2026-07-14 09:30:00+00"
    assert marks[0]["reason"] == "inventory reports the instance deleted"
    assert clears == []


def test_a_replaced_endpoint_is_marked_now_and_says_where_it_went():
    servers = [_srv("svc-b", "abc123.region.aws.clickhouse.cloud")]
    marks, _ = marks_for(servers, [_t(2, "svc-b", "svc-b.x.rds.example.com")])
    assert marks[0]["deleted_at"] is None            # None means "now"
    assert "different endpoint" in marks[0]["reason"]
    assert "clickhouse.cloud" in marks[0]["reason"]


def test_marks_do_not_care_whether_the_target_is_enabled():
    """Step 3 disables enabled targets only; a target that was already disabled
    when its instance went away still has to be marked."""
    servers = [_srv("svc-a", "svc-a.x.example.com", deleted=True)]
    rows = [_t(1, "svc-a", "svc-a.x.example.com")]      # no enabled key at all
    assert [m["id"] for m in marks_for(servers, rows)[0]] == [1]


def test_an_already_marked_target_is_not_marked_again():
    servers = [_srv("svc-a", "svc-a.x.example.com", deleted=True)]
    rows = [_t(1, "svc-a", "svc-a.x.example.com", deleted_at="2026-07-14")]
    assert marks_for(servers, rows) == ([], [])


def test_a_marked_target_whose_endpoint_is_live_again_is_cleared():
    ep = "mail.internal.example.com"
    servers = [_srv("mail", ep, deleted=True), _srv("mail", ep)]
    marks, clears = marks_for(servers, [_t(9, "mail", ep, deleted_at="2026-08-01")])
    assert marks == []
    assert clears == [{"id": 9, "alias": "mail", "host": ep}]


def test_plain_absence_marks_nothing_and_a_hostless_target_is_skipped():
    servers = [_srv("other", "other.x.example.com")]
    rows = [_t(3, "outside", "outside.y.example.com"), _t(4, "archive", None),
            _t(5, "blank", "")]
    assert marks_for(servers, rows) == ([], [])
