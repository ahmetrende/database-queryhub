"""The two metrics implementations must agree, and now something checks.

`web/metrics.py` aggregates in Python for the admin panel; the static S3
dashboard ships raw rows and aggregates in embedded JavaScript so it can
re-filter in the browser with no server. Both are needed — the second one's
offline filtering is the reason it exists — but until now nothing verified they
produced the same numbers from the same inputs, and the cost formula existed
twice in two languages.

The definitions moved into `metrics_defs.py`. The JS arithmetic necessarily
stays in the dashboard, so this test extracts that function from the generated
page and runs it in node against the Python implementation over the same
configs. If someone changes one side's rounding or coefficient handling, the
build fails here instead of the two dashboards quietly disagreeing.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from queryhub import metrics_defs

BUILDER = Path(__file__).resolve().parents[1] / "scripts" / "build_metrics_dashboard.py"

# Configs to compare on: the shipped defaults, an all-zero install, a big
# deployment, and the values that actually break naive code — blanks, junk and
# fractions.
CASES = [
    (120, {"cost_dba_minutes_per_request": "12", "cost_dba_hourly_usd": "60",
           "cost_avoided_replicas": "2", "cost_per_replica_monthly_usd": "450",
           "cost_other_monthly_usd": "120"}),
    (0, {"cost_dba_minutes_per_request": "12", "cost_dba_hourly_usd": "60",
         "cost_avoided_replicas": "0", "cost_per_replica_monthly_usd": "0",
         "cost_other_monthly_usd": "0"}),
    (98_765, {"cost_dba_minutes_per_request": "7.5", "cost_dba_hourly_usd": "85.25",
              "cost_avoided_replicas": "11", "cost_per_replica_monthly_usd": "612.40",
              "cost_other_monthly_usd": "1999.99"}),
    (7, {"cost_dba_minutes_per_request": "", "cost_dba_hourly_usd": "60",
         "cost_avoided_replicas": "1", "cost_per_replica_monthly_usd": "450",
         "cost_other_monthly_usd": ""}),
    (7, {}),                                   # nothing configured at all
    (33, {"cost_dba_minutes_per_request": "not-a-number",
          "cost_dba_hourly_usd": "60"}),       # operator typo
]


def _js_cost_function():
    """Lift `FACTORIES.kpiCostSavings`'s arithmetic out of the builder.

    Only the computation is taken, not the rendering: renderKPIs touches the
    DOM. If the function is renamed or restructured this raises, which is the
    correct outcome — it means the thing under comparison moved.
    """
    src = BUILDER.read_text(encoding="utf-8")
    start = src.index("FACTORIES.kpiCostSavings = function")
    end = src.index("renderKPIs(", start)
    body = src[start:end]

    lines = [ln for ln in body.split("\n")
             if "const" in ln or "rows.filter" in ln]
    assert any("dbaHoursSaved" in ln for ln in lines), \
        "the JS cost computation no longer looks the way this test lifts it"
    return "\n".join(lines)


def _run_js(completed, config):
    """Evaluate the dashboard's JS formula for one case and return its numbers,
    rounded exactly as the page displays them."""
    js = _js_cost_function()
    script = f"""
const DATA = {{ config: {json.dumps(config)} }};
const rows = Array.from({{length: {completed}}}, () => ({{status: 'completed'}}));
{js}
console.log(JSON.stringify({{
  completed: completed,
  dbaHoursSaved: Number(dbaHoursSaved.toFixed(1)),
  dbaSavingUsd: Math.round(dbaSavingUSD),
  infraUsdPerMonth: Math.round(monthlyInfra),
}}));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("completed,config", CASES,
                         ids=[f"case{i}" for i in range(len(CASES))])
def test_python_and_js_cost_formulas_agree(completed, config):
    py = metrics_defs.cost_savings(completed, config)
    js = _run_js(completed, config)
    for key in ("completed", "dbaHoursSaved", "dbaSavingUsd", "infraUsdPerMonth"):
        assert py[key] == js[key], (
            f"{key}: python={py[key]} js={js[key]} for completed={completed} "
            f"config={config}")


def test_both_consumers_read_the_same_config_keys():
    """The web query and the dashboard query must select the same keys. They
    did, by coincidence; now they share the list."""
    web = (Path(__file__).resolve().parents[1] / "src" / "queryhub"
           / "web" / "metrics.py").read_text(encoding="utf-8")
    assert "metrics_defs.CONFIG_KEYS" in web

    builder = BUILDER.read_text(encoding="utf-8")
    assert "metrics_defs.config_key_sql_list()" in builder
    # And no hand-written duplicate of the key list survived in either file.
    for name, text in (("web/metrics.py", web), ("builder", builder)):
        assert text.count("'cost_dba_minutes_per_request'") + \
            text.count('"cost_dba_minutes_per_request"') == 0, \
            f"{name} still hardcodes a cost key"


def test_terminal_statuses_have_one_definition():
    web = (Path(__file__).resolve().parents[1] / "src" / "queryhub"
           / "web" / "metrics.py").read_text(encoding="utf-8")
    assert "metrics_defs.TERMINAL_STATUSES" in web
    assert '("completed", "failed", "rejected", "cancelled")' not in web


def test_coerce_float_matches_javascript_parsefloat_on_junk():
    """parseFloat('') is NaN in JS and the dashboard `|| 0`s it; Python's float('')
    raises. Both must land on 0.0, or a blank coefficient shows different numbers
    on the two pages."""
    for junk in ("", None, "abc", "  "):
        assert metrics_defs.coerce_float(junk) == 0.0


def test_views_list_matches_what_both_query():
    builder = BUILDER.read_text(encoding="utf-8")
    web = (Path(__file__).resolve().parents[1] / "src" / "queryhub"
           / "web" / "metrics.py").read_text(encoding="utf-8")
    for view in metrics_defs.VIEWS:
        assert view in builder, f"{view} not queried by the dashboard builder"
    # The web module reads a subset (it does not ship the raw import rows), so
    # only assert the primary fact view here.
    assert "p_metrics_request_facts" in web


def test_regex_extraction_is_pinned_to_a_real_function():
    """Guard the guard: if the lift silently returned nothing, every parity case
    above would pass on an empty formula."""
    js = _js_cost_function()
    assert "dbaHoursSaved" in js and "monthlyInfra" in js
    assert len(re.findall(r"const ", js)) >= 5
