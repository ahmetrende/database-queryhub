"""A period with no data shows no rate, not 0%.

The IdP's first KPI chart drew "auto-approval rate 0%" for every week before
its event capture started, then a jump to 99%. The rate had been 86-95% all
along. QueryHub had the same fault: the S3 dashboard filled empty weeks with
0 in four weekly series, and the in-app headline said 0% for an empty window.
An empty period is now null (a gap in a chart, "—" on a card).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from queryhub.web import metrics

BUILDER = Path(__file__).resolve().parents[1] / "scripts" / "build_metrics_dashboard.py"


def test_the_in_app_headline_has_no_rate_for_an_empty_window(monkeypatch):
    monkeypatch.setattr(metrics.db, "fetch_all", lambda sql, params=None: [])
    monkeypatch.setattr(metrics.people, "display_names", lambda ids: {}, raising=False)
    out = metrics.build_metrics()
    h = out["headline"]
    assert h["total"] == 0
    assert h["autoApproveRate"] is None
    assert h["successRate"] is None
    assert h["rejectRate"] is None
    # The old keys carry the same answer.
    assert out["autoApproveRate"] is None and out["rejectRate"] is None


def _lift(src: str, opener: str) -> str:
    """The source of one top-level JS function, from its opening line to the
    first line that closes it at column 0."""
    start = src.index(opener)
    end = re.compile(r"^\};?$", re.M).search(src, start).end()
    return src[start:end]


FACTORIES = ("failureBreakdown", "scheduledUsage", "ratingWeekly", "ratingResponse")


def _weekly_series():
    """Run the four weekly factories of the dashboard in node over requests in
    two weeks with an empty week between them. Returns {factory: datasets}."""
    src = BUILDER.read_text(encoding="utf-8")
    parts = [_lift(src, "function truncWeek("), _lift(src, "function weeksBetween("),
             _lift(src, "function gapFilledBuckets(")]
    parts += [_lift(src, f"FACTORIES.{f} = function") for f in FACTORIES]
    rows = [
        {"created_at": "2026-10-05T10:00:00Z", "status": "completed",
         "scheduled_for": None, "rating": 5},
        {"created_at": "2026-10-19T10:00:00Z", "status": "completed",
         "scheduled_for": "2026-10-19T12:00:00Z", "rating": 4},
    ]
    script = (
        "const C = {}; const FACTORIES = {};\n"
        "const annotationsFor = () => [];\n"
        "const lineSpec = (labels, datasets) => ({ labels, datasets });\n"
        "const stackSpec = (labels, datasets, opts) =>"
        " ({ labels, datasets: datasets.concat(opts.overlay ? [opts.overlay] : []) });\n"
        + "\n".join(parts) + "\n"
        + f"const rows = {json.dumps(rows)};\n"
        + "const out = {};\n"
        + "".join(f"out.{f} = FACTORIES.{f}(rows);\n" for f in FACTORIES)
        + "console.log(JSON.stringify(out));\n")
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_s3_dashboard_draws_a_gap_for_an_empty_week():
    out = _weekly_series()
    want = {"failureBreakdown": "success %", "scheduledUsage": "scheduled %",
            "ratingWeekly": "avg rating", "ratingResponse": "response %"}
    for factory, label in want.items():
        spec = out[factory]
        assert spec["labels"] == ["2026-10-05", "2026-10-12", "2026-10-19"], factory
        data = next(d["data"] for d in spec["datasets"] if d["label"] == label)
        assert data[1] is None, f"{factory}: the empty week must be a gap, got {data[1]}"
        assert data[0] is not None and data[2] is not None, factory
    # A week WITH data keeps its real value, 0 included.
    sched = next(d["data"] for d in out["scheduledUsage"]["datasets"]
                 if d["label"] == "scheduled %")
    assert sched == [0, None, 100]
