"""Approval latency and admin workload count decisions taken by people.

5,776 of 6,913 decisions in the live data (2026-09-28) were auto-approvals: a
grant decided them in about no time. Counted in, every latency percentile sat
near zero, and the admin-workload chart of the S3 dashboard led with an "AUTO"
bar ten times the size of anyone's. Both dashboards now leave them out of the
latency and the workload, and the S3 one shows their count as its own card.
The rows carry `auto_approved` from p_metrics_request_facts (migration 135).
"""
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

from queryhub.web import metrics

BUILDER = Path(__file__).resolve().parents[1] / "scripts" / "build_metrics_dashboard.py"
T0 = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)


def _row(i, auto, sec, decider):
    return {"id": i, "created_at": T0 + timedelta(minutes=i), "status": "completed",
            "requester_slack_id": "U0EXAMPLE009", "requester_name": "Jordan Ray",
            "team": "Metrics Pod", "target_alias": "prod-main", "database_name": "app",
            "tier": "ro", "decided_by_slack_id": decider, "decided_by_name": decider,
            "approval_sec": sec, "exec_sec": 0.5, "hour_local": 12, "dow_local": 1,
            "rating": None, "scheduled_for": None, "auto_approved": auto,
            "decided_at": T0, "executed_at": T0, "completed_at": T0}


def test_the_in_app_percentiles_are_people_only(monkeypatch):
    rows = [_row(i, True, 0.1, "AUTO") for i in range(20)]
    rows += [_row(100, False, 600.0, "U0EXAMPLE001"), _row(101, False, 1200.0, "U0EXAMPLE001")]

    def fetch_all(sql, params=None):
        return rows if "p_metrics_request_facts" in sql else []
    monkeypatch.setattr(metrics.db, "fetch_all", fetch_all)
    monkeypatch.setattr(metrics.people, "display_names", lambda ids: {}, raising=False)
    out = metrics.build_metrics()
    assert out["totalQueries"] == 22
    assert out["headline"]["p50ApprovalSec"] == 900.0, "median of the two people"
    assert out["avgLatencyMin"] == 15.0
    assert out["headline"]["autoApproveRate"] > 0.9, "still counted, as their own figure"


def test_the_s3_dashboard_leaves_them_out_of_latency_and_workload():
    src = BUILDER.read_text(encoding="utf-8")
    helper = src[src.index("function humanApprovalSecs"):src.index("FACTORIES.approvalSla")]
    assert "!r.auto_approved" in helper
    sla = src[src.index("FACTORIES.approvalSla"):src.index("FACTORIES.businessOffhours")]
    assert sla.count("humanApprovalSecs(") == 3
    work = src[src.index("FACTORIES.adminWorkload"):src.index("FACTORIES.targetHeatmap")]
    assert "!r.auto_approved" in work
    kpi = src[src.index("FACTORIES.kpi = "):src.index("FACTORIES.kpiCostSavings")]
    assert "humanApprovalSecs(rows)" in kpi and "'Auto-approved'" in kpi


def test_the_in_app_module_reads_the_flag_for_both_latency_lists():
    src = inspect.getsource(metrics.build_metrics)
    assert src.count('not r.get("auto_approved")') == 2
