"""The read-burst nudge counts submitted reads, not opened tabs.

A user ran ONE query and the bot answered "you've run 3 read queries in the
last 10 min", offering a window for `target #None (all dbs)`. Both halves came
from the same row type: opening the /sql modal reserves an id as a `draft` —
no target, no database, no SQL — and the counter read `requests` directly
instead of through the reportable view that every other caller uses.

Two drafts and one real request made three. The scope label then took the
most common (target, database) pair, which was the drafts' (NULL, ''), and
`targets.get(None)` fell through to the id-shaped fallback.

Measured on the real rows: two drafts a minute before the request that
triggered the nudge.
"""
from queryhub.slack_app import modal


def _rows(monkeypatch, rows):
    captured = {}

    def fake_fetch_all(sql, params=None):
        captured["sql"] = sql
        return rows
    monkeypatch.setattr(modal.db, "fetch_all", fake_fetch_all)
    return captured


def _req(tid=15, db="gopanel_service", q="SELECT 1"):
    return {"target_server_id": tid, "database_name": db, "query": q}


def _draft():
    # Exactly what core_submit.reserve writes: no target, no database, no SQL.
    return {"target_server_id": None, "database_name": "", "query": ""}


def test_one_real_query_with_two_open_tabs_is_not_a_burst(monkeypatch):
    _rows(monkeypatch, [_draft(), _draft(), _req()])
    assert modal._recent_ro_burst("U_READER") is None


def test_three_real_reads_are_a_burst(monkeypatch):
    _rows(monkeypatch, [_req(), _req(), _req()])
    burst = modal._recent_ro_burst("U1")
    assert burst == {"count": 3, "target_server_id": 15,
                     "database_name": "gopanel_service"}


def test_the_draft_filter_is_in_the_sql(monkeypatch):
    """Not in Python: `LIMIT 50` would otherwise fill with abandoned tabs and
    push the real requests out of the window entirely."""
    captured = _rows(monkeypatch, [_req(), _req(), _req()])
    modal._recent_ro_burst("U1")
    assert "status <> 'draft'" in captured["sql"]
    assert "target_server_id IS NOT NULL" in captured["sql"]


def test_a_row_with_no_target_never_reaches_the_scope_label(monkeypatch):
    """Belt and braces for the `target #None (all dbs)` line: even if such a
    row arrives, it must not win the tally."""
    _rows(monkeypatch, [_draft(), _req(), _req(), _req()])
    burst = modal._recent_ro_burst("U1")
    assert burst["target_server_id"] == 15
    assert burst["count"] == 3


def test_an_empty_query_is_not_counted_as_a_read(monkeypatch):
    # required_mode('') == 'ro' — the right fail-safe there, the wrong count
    # here. Three blank rows are not three reads.
    assert modal.query_safety.required_mode("") == "ro"
    _rows(monkeypatch, [_req(q=""), _req(q="   "), _req()])
    assert modal._recent_ro_burst("U1") is None


def test_a_write_does_not_count_toward_a_read_burst(monkeypatch):
    _rows(monkeypatch, [_req(q="UPDATE t SET a = 1 WHERE id = 2"), _req(), _req()])
    assert modal._recent_ro_burst("U1") is None


def test_the_busiest_target_wins_the_scope(monkeypatch):
    _rows(monkeypatch, [_req(tid=15), _req(tid=15), _req(tid=52, db="nova")])
    assert modal._recent_ro_burst("U1")["target_server_id"] == 15
