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
