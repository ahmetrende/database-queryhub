"""The metrics dashboard shows the channel mix (migration 200).

`origin` names the door a request came through (slack, web, idp, mcp). The page
aggregates in the browser, so the aggregation is lifted out of the generated
page and run in node against an independent Python count of the same rows. A
negative control then breaks the lifted code in four ways and requires this
comparison to fail for each one: a comparison that no broken copy can fail
proves nothing.
"""
import importlib.util
import json
import shutil
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_metrics_dashboard.py"
MIGRATION = ROOT / "migrations" / "200_metrics_facts_origin.sql"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)   # a Monday


def _row(i, user, origin, days=0):
    created = (T0 + timedelta(days=days, minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": i, "created_at": created, "requester_slack_id": user,
            "origin": origin, "status": "completed"}


ROWS = [
    _row(1, "U1", "slack"), _row(2, "U1", "slack"), _row(3, "U1", "web", 1),
    _row(4, "U2", "web", 1), _row(5, "U2", "web", 8), _row(6, "U3", "slack", 8),
    _row(7, "U3", "idp", 15), _row(8, "U3", "web", 15), _row(9, "U4", "  Web "),
    _row(10, "U5", None, 2), _row(11, "U6", "", 3), _row(12, "U7", "carrier-pigeon", 3),
]


# ---- the independent count ------------------------------------------------

KNOWN = ["slack", "web", "idp", "mcp"]
LABEL = {"slack": "Slack", "web": "Web", "idp": "IdP", "mcp": "MCP"}


def _key(origin):
    return (origin or "").strip().lower() or "slack"


def _rank(key):
    return KNOWN.index(key) if key in KNOWN else len(KNOWN)


def _share(part, whole):
    return round(1000 * part / whole) / 10 if whole else 0


def expected(rows):
    requests = defaultdict(int)
    users = defaultdict(set)
    chans = defaultdict(set)
    for r in rows:
        k = _key(r["origin"])
        requests[k] += 1
        users[k].add(r["requester_slack_id"])
        chans[r["requester_slack_id"]].add(k)
    total_users = len(chans)
    channels = [{"key": k, "label": LABEL.get(k, k), "requests": requests[k],
                 "requestShare": _share(requests[k], len(rows)), "users": len(users[k]),
                 "userShare": _share(len(users[k]), total_users)}
                for k in sorted(requests, key=lambda k: (_rank(k), k))]
    segs = defaultdict(int)
    for ks in chans.values():
        segs[" + ".join(LABEL.get(k, k) for k in sorted(ks, key=lambda k: (_rank(k), k)))] += 1
    segments = [{"label": label, "users": n}
                for label, n in sorted(segs.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"channels": channels, "totalRequests": len(rows), "totalUsers": total_users,
            "segments": segments}


def expected_weekly(rows):
    def monday(created):
        d = datetime.strptime(created[:10], "%Y-%m-%d").date()
        return d - timedelta(days=d.weekday())
    weeks = sorted({monday(r["created_at"]) for r in rows})
    first, last = weeks[0], weeks[-1]
    labels = []
    cur = first
    while cur <= last:
        labels.append(cur.isoformat())
        cur += timedelta(days=7)
    out = {}
    for ch in expected(rows)["channels"]:
        mine = [r for r in rows if _key(r["origin"]) == ch["key"]]
        out[ch["label"]] = {
            "requests": [sum(1 for r in mine if monday(r["created_at"]).isoformat() == w)
                         for w in labels],
            "people": [len({r["requester_slack_id"] for r in mine
                            if monday(r["created_at"]).isoformat() == w}) for w in labels]}
    return labels, out


# ---- lifting the page's own code -------------------------------------------

def _builder():
    spec = importlib.util.spec_from_file_location("build_metrics_dashboard", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _renderer_js():
    return _builder().RENDERER_JS


def _function(src, name):
    start = src.index(f"function {name}(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[start:j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _lifted(mutate=None):
    src = _renderer_js()
    consts = src[src.index("const CHANNELS = ["):src.index("function channelKey(")]
    parts = [consts] + [_function(src, n) for n in (
        "channelKey", "channelInfo", "oneDecimal", "channelMixStats",
        "truncWeek", "weeksBetween", "gapFilledBuckets", "channelWeeklySeries")]
    js = "\n".join(parts)
    if mutate:
        old, new = mutate
        assert old in js, f"mutation target vanished: {old!r}"
        js = js.replace(old, new)
    return js


def _run(rows, mutate=None):
    script = _lifted(mutate) + f"""
const rows = {json.dumps(rows)};
const weekly = (fn) => channelWeeklySeries(rows, fn);
const w1 = weekly(rs => rs.length), w2 = weekly(rs => new Set(rs.map(r => r.requester_slack_id)).size);
const named = (w) => Object.fromEntries(w.datasets.map(d => [d.label, d.data]));
console.log(JSON.stringify({{
  mix: channelMixStats(rows), labels: w1.labels,
  requests: named(w1), people: named(w2),
  empty: channelMixStats([]),
}}));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _strip_colors(mix):
    return {**mix, "channels": [{k: v for k, v in c.items() if k != "color"}
                                for c in mix["channels"]]}


def _agrees(rows, mutate=None):
    got = _run(rows, mutate)
    labels, weekly = expected_weekly(rows)
    return (_strip_colors(got["mix"]) == expected(rows)
            and got["labels"] == labels
            and got["requests"] == {k: v["requests"] for k, v in weekly.items()}
            and got["people"] == {k: v["people"] for k, v in weekly.items()})


# ---- the tests --------------------------------------------------------------

@needs_node
def test_the_page_counts_requests_and_distinct_people_per_channel():
    got = _run(ROWS)
    assert _strip_colors(got["mix"]) == expected(ROWS)
    mix = {c["key"]: c for c in got["mix"]["channels"]}
    assert (mix["slack"]["requests"], mix["slack"]["users"]) == (5, 4)   # a null and a blank origin count as slack
    assert (mix["web"]["requests"], mix["web"]["users"]) == (5, 4)       # "  Web " counts as web
    assert (mix["idp"]["requests"], mix["idp"]["users"]) == (1, 1)
    assert got["mix"]["totalUsers"] == 7, "a person on two channels counts once in the total"
    assert sum(c["users"] for c in got["mix"]["channels"]) > got["mix"]["totalUsers"]


@needs_node
def test_known_channels_come_first_and_an_unknown_one_shows_under_its_own_name():
    keys = [c["key"] for c in _run(ROWS)["mix"]["channels"]]
    assert keys == ["slack", "web", "idp", "carrier-pigeon"]


@needs_node
def test_people_are_grouped_by_the_channels_they_used():
    segments = {s["label"]: s["users"] for s in _run(ROWS)["mix"]["segments"]}
    assert segments == {"Slack": 2, "Web": 2, "Slack + Web": 1, "Slack + Web + IdP": 1,
                        "carrier-pigeon": 1}
    assert _run(ROWS)["mix"]["segments"] == expected(ROWS)["segments"]


@needs_node
def test_the_weekly_trend_has_one_point_per_week_without_gaps():
    got = _run(ROWS)
    assert got["labels"] == ["2026-10-12", "2026-10-19", "2026-10-26"]
    assert got["requests"]["IdP"] == [0, 0, 1], "a channel that starts late is zero before it"
    assert got["people"]["Web"] == [3, 1, 1]


@needs_node
def test_no_rows_means_no_channels_not_a_crash():
    empty = _run(ROWS)["empty"]
    assert empty["channels"] == [] and empty["totalRequests"] == 0 and empty["totalUsers"] == 0


@needs_node
@pytest.mark.parametrize("name,mutation", [
    ("people counted as requests", ("users: p.users.size,", "users: p.requests,")),
    ("blank origin dropped instead of read as slack",
     ("|| 'slack';\n}", "|| 'unknown';\n}")),
    ("origin case not folded", (".trim().toLowerCase()", ".trim()")),
    ("total people counted per channel", ("const totalUsers = byUser.size;",
                                          "const totalUsers = rows.length;")),
])
def test_negative_control_each_broken_copy_is_caught(name, mutation):
    assert _agrees(ROWS), "the unbroken copy must agree, or this control proves nothing"
    assert not _agrees(ROWS, mutation), f"mutant survived: {name}"


def test_the_page_carries_the_panel_and_the_view_carries_the_column():
    page = _builder().render({"rows": [], "annotations": [], "teams": [], "users": [], "targets": [],
                       "databases": [], "who_can_what": [], "rating_low": [], "csv_imports": [],
                       "config": {}, "generated_at": "x"})
    for spec_id in ("channel-mix", "channel-weekly", "channel-users-weekly"):
        assert f"id='canvas-{spec_id}'" in page
    assert "id='grp-channels'" in page
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "AS origin" in sql and "CREATE OR REPLACE VIEW p_metrics_request_facts" in sql
