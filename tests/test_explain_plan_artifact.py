"""A lone EXPLAIN is delivered as a Slack code block AND stored as a file.

The code block is a Slack channel. A web-origin request has none: the UI fetches
`requests.csv_file_path`, and this path never set it — so the request finished
`completed` with a row count in the header and nothing to show. Measured on
production: ten requests from two people between 2026-08-05 and 2026-08-21 had
their plan discarded (audit `delivery: code_block`, no artifact on disk, and no
DM either because `web_result_to_slack` is off for web-origin requests).

So the plan is now written to a one-column result file as well. Slack keeps the
code block; every other transport has something to read.
"""
from contextlib import contextmanager

import pytest

from queryhub import executor as ex

PLAN = [
    "Limit  (cost=0.00..1.23 rows=5000 width=8) (actual time=0.011..1.902 rows=5000 loops=1)",
    "  ->  Seq Scan on notification_batch_recipient  (cost=0.00..99.9 rows=90000 width=8)",
    "        Filter: (created_at < '2026-07-20'::date)",
    "        Rows Removed by Filter: 12",
    "Planning Time: 0.104 ms",
    "Execution Time: 2.031 ms",
]


class _Col:
    """Shape of psycopg3's Column: indexable AND attribute-bearing."""

    def __init__(self, name, type_display="text"):
        self.name = name
        self.type_display = type_display
        self.type_code = 25
        self.null_ok = None

    def __getitem__(self, i):
        return (self.name, self.type_code, None, None, None, None,
                self.null_ok)[i]

    def __len__(self):
        return 7


class _PlanCur:
    """Mimics psycopg for a streamed EXPLAIN: one 'QUERY PLAN' text column."""
    rowcount = -1

    def __init__(self, lines=PLAN):
        self._lines = lines
        self.description = [_Col("QUERY PLAN")]

    def stream(self, sql):
        return iter([(ln,) for ln in self._lines])

    def execute(self, sql, prepare=False):   # pragma: no cover - not used
        raise AssertionError("EXPLAIN must stream")


def _stmt(sql="EXPLAIN (ANALYZE, BUFFERS) SELECT id FROM t"):
    return type("S", (), {"rewritten": sql, "leading": "EXPLAIN"})()


def _run(cur, tmp_path, monkeypatch, *, wants_result=True, max_rows=100000,
         explain_max_chars=11000):
    monkeypatch.setattr(ex, "CSV_DIR", tmp_path)
    monkeypatch.setattr(ex.stmt_guard, "check", lambda *a, **k: None)
    monkeypatch.setattr(ex.pii, "is_enabled", lambda: False)
    monkeypatch.setattr(
        ex.cfg, "get_int",
        lambda key, default=0: (explain_max_chars
                                if key == "explain_max_chars" else default))
    return ex._execute_main_statement(
        cur, _stmt(), 1, 4830, wants_result, max_rows, 1_000_000,
        capture_plan=True, force_extended=True,
    )


def _file_rows(path):
    import csv
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


# --- the capture writes both channels ---------------------------------------

def test_lone_explain_writes_the_plan_as_a_result_file(tmp_path, monkeypatch):
    res = _run(_PlanCur(), tmp_path, monkeypatch)

    # Slack's channel is unchanged.
    assert res.plan_text.splitlines() == PLAN
    # And the web's channel now exists.
    assert res.csv_path is not None
    rows = _file_rows(res.csv_path)
    assert rows[0] == ["QUERY PLAN"]
    assert [r[0] for r in rows[1:]] == PLAN
    # The count reported to the requester is the plan's real length.
    assert res.rowcount == len(PLAN)
    assert res.truncated_rows is False


def test_a_plan_clipped_for_slack_is_still_whole_in_the_file(tmp_path,
                                                             monkeypatch):
    # explain_max_chars is a Slack message limit. It used to cut the only copy
    # of the plan that existed; it must not cut the file.
    res = _run(_PlanCur(), tmp_path, monkeypatch, explain_max_chars=120)

    assert res.truncated_rows is True                    # the block is clipped
    assert len(res.plan_text.splitlines()) < len(PLAN)
    assert [r[0] for r in _file_rows(res.csv_path)[1:]] == PLAN
    assert res.rowcount == len(PLAN)


def test_no_file_when_the_requester_asked_for_no_result(tmp_path, monkeypatch):
    # `wants_result=False` is an explicit "don't produce a file". The plan
    # still goes out inline, which is why this branch runs before the flag.
    res = _run(_PlanCur(), tmp_path, monkeypatch, wants_result=False)
    assert res.plan_text.splitlines() == PLAN
    assert res.csv_path is None
    assert list(tmp_path.iterdir()) == []


def test_a_plan_past_the_row_cap_is_reported_as_truncated(tmp_path,
                                                          monkeypatch):
    res = _run(_PlanCur(), tmp_path, monkeypatch, max_rows=2)
    assert [r[0] for r in _file_rows(res.csv_path)[1:]] == PLAN[:2]
    assert res.truncated_rows is True


def test_json_format_plan_gets_a_named_column(tmp_path, monkeypatch):
    # FORMAT JSON arrives as one cell; the header still has to say something.
    cur = _PlanCur(['[{"Plan": {"Node Type": "Limit"}}]'])
    cur.description = [_Col("QUERY PLAN"), _Col("extra")]
    cur.stream = lambda sql: iter([('[{"Plan": {}}]', None)])
    res = _run(cur, tmp_path, monkeypatch)
    assert _file_rows(res.csv_path)[0] == [ex._PLAN_COLUMN]


# --- _read_plan_text keeps every line --------------------------------------

def test_read_plan_text_returns_all_lines_and_a_clipped_view(monkeypatch):
    monkeypatch.setattr(ex.cfg, "get_int", lambda key, default=0: 20)
    rows = [("x" * 10,), ("y" * 10,), ("z" * 10,)]
    text, lines, trunc = ex._read_plan_text(iter(rows), ["QUERY PLAN"])
    assert text == "x" * 10          # only what fits the Slack budget
    assert len(lines) == 3           # the file gets the whole plan
    assert trunc is True


def test_read_plan_text_stops_at_the_size_cap():
    rows = [("x" * 100,)] * 50
    _text, lines, trunc = ex._read_plan_text(
        iter(rows), ["QUERY PLAN"], max_bytes=250)
    assert len(lines) == 2           # 101 bytes each; the third crosses 250
    assert trunc is True


# --- delivery records the artifact -----------------------------------------

@pytest.fixture
def captured(monkeypatch):
    state = {"sql": [], "params": (), "audit": {}}

    class _Cur:
        def execute(self, sql, params=None):
            state["sql"].append(sql)
            state["params"] = params

    @contextmanager
    def fake_txn():
        yield _Cur()

    monkeypatch.setattr(ex.db, "transaction", fake_txn)
    monkeypatch.setattr(ex.audit, "log_in",
                        lambda c, r, u, n, a, d: state["audit"].update(d))
    monkeypatch.setattr(ex, "_deliver_result_to_requester", lambda r: False)
    monkeypatch.setattr(ex.notifications, "update_all_admin_messages",
                        lambda *a, **k: None)
    monkeypatch.setattr(ex.notifications, "request_context_md", lambda r: "")
    monkeypatch.setattr(ex, "_fmt_approve_ts", lambda r: "")
    return state


def test_completion_stores_the_plan_file_path(captured):
    request = {"id": 4830, "requester_slack_id": "U1", "origin": "web",
               "decided_by_slack_id": "AUTO", "database_name": "d",
               "target_server_id": 21}
    ex._complete_with_plan(None, request, "\n".join(PLAN[:2]), False,
                           elapsed=0.04, csv_path="/results/req_4830_q1.csv",
                           line_count=len(PLAN))

    assert "csv_file_path = %s" in captured["sql"][0]
    assert captured["params"][2] == "/results/req_4830_q1.csv"
    # The count is the plan's length, not the code block's.
    assert captured["params"][0] == len(PLAN)
    assert captured["audit"]["delivery"] == "code_block+file"
    assert captured["audit"]["explain_plan_lines"] == len(PLAN)


def test_completion_without_a_file_is_unchanged(captured):
    request = {"id": 99, "requester_slack_id": "U1", "origin": "slack",
               "decided_by_slack_id": "AUTO", "database_name": "d",
               "target_server_id": 21}
    ex._complete_with_plan(None, request, "one line", False, elapsed=0.01)
    assert captured["params"][2] is None
    assert captured["audit"]["delivery"] == "code_block"
