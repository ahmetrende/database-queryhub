"""executor — pure helper functions (formatting + txn-compat classifier)."""
import pytest
from slack_sdk.errors import SlackApiError

from queryhub import executor as ex


class _FakeUploadClient:
    """files_upload_v2 that raises the given errors on successive calls,
    then succeeds. Error strings become SlackApiError(ok:false)."""
    def __init__(self, fail_errors):
        self.fail_errors = list(fail_errors)
        self.calls = 0

    def files_upload_v2(self, **kwargs):
        self.calls += 1
        if self.fail_errors:
            raise SlackApiError("boom", {"error": self.fail_errors.pop(0)})
        return {"files": [{"id": "F1"}]}


def test_upload_retry_first_try(monkeypatch):
    monkeypatch.setattr(ex.time, "sleep", lambda s: None)
    c = _FakeUploadClient([])
    assert ex._upload_with_retry(c, channel="C")["files"][0]["id"] == "F1"
    assert c.calls == 1


def test_upload_retry_recovers_from_transient(monkeypatch):
    monkeypatch.setattr(ex.time, "sleep", lambda s: None)
    c = _FakeUploadClient(["file_update_failed", "file_update_failed"])
    assert ex._upload_with_retry(c, channel="C")["files"][0]["id"] == "F1"
    assert c.calls == 3   # 2 transient failures, 3rd succeeds


def test_upload_retry_gives_up_after_max(monkeypatch):
    monkeypatch.setattr(ex.time, "sleep", lambda s: None)
    c = _FakeUploadClient(["file_update_failed"] * 5)
    with pytest.raises(SlackApiError):
        ex._upload_with_retry(c, channel="C")
    assert c.calls == 3   # capped at 3 attempts total


def test_upload_retry_reraises_non_transient(monkeypatch):
    monkeypatch.setattr(ex.time, "sleep", lambda s: None)
    c = _FakeUploadClient(["not_in_channel"])
    with pytest.raises(SlackApiError):
        ex._upload_with_retry(c, channel="C")
    assert c.calls == 1   # real error → no retry


def test_size_cap_alert_dms_every_admin(monkeypatch):
    sent = []
    monkeypatch.setattr(ex.admins, "list_active",
                        lambda: [{"slack_user_id": "A1"}, {"slack_user_id": "A2"}])
    monkeypatch.setattr(ex.notifications, "dm_requester",
                        lambda client, uid, text: sent.append((uid, text)))
    monkeypatch.setattr(ex.targets, "get", lambda tid: None)
    ex._alert_admins_size_cap(
        object(),
        {"id": 7, "requester_slack_id": "U9", "target_server_id": 5,
         "database_name": "db"},
        100)
    assert [u for u, _ in sent] == ["A1", "A2"]      # every admin
    assert all("100 MB" in t and "#7" in t for _, t in sent)


# --- _is_txn_incompatible ---------------------------------------------------
#
# These commands Postgres refuses to run inside a transaction block. As the
# sole statement they run in autocommit; alongside others they escalate to
# manual DBA. A false negative here is a real failure mode (the #257 bug).

def test_create_index_concurrently_incompatible():
    assert ex._is_txn_incompatible("CREATE INDEX CONCURRENTLY idx ON t (a)")


def test_drop_index_concurrently_incompatible():
    assert ex._is_txn_incompatible("DROP INDEX CONCURRENTLY idx")


def test_vacuum_incompatible():
    assert ex._is_txn_incompatible("VACUUM ANALYZE t")


def test_create_database_incompatible():
    assert ex._is_txn_incompatible("CREATE DATABASE foo")


def test_alter_system_incompatible():
    assert ex._is_txn_incompatible("ALTER SYSTEM SET work_mem = '256MB'")


def test_case_insensitive():
    assert ex._is_txn_incompatible("create index concurrently idx on t (a)")


def test_plain_select_compatible():
    assert not ex._is_txn_incompatible("SELECT 1")


def test_plain_create_index_compatible():
    # Without CONCURRENTLY a CREATE INDEX is fine inside a transaction.
    assert not ex._is_txn_incompatible("CREATE INDEX idx ON t (a)")


def test_update_compatible():
    assert not ex._is_txn_incompatible("UPDATE t SET a = 1 WHERE id = 2")


def test_empty_compatible():
    assert not ex._is_txn_incompatible("")
    assert not ex._is_txn_incompatible(None)


# --- _fmt_count -------------------------------------------------------------

def test_fmt_count_thousands():
    assert ex._fmt_count(0) == "0"
    assert ex._fmt_count(999) == "999"
    assert ex._fmt_count(1000) == "1,000"
    assert ex._fmt_count(6_990_295) == "6,990,295"


# --- _fmt_duration ----------------------------------------------------------

def test_fmt_duration_subsecond_ms():
    assert ex._fmt_duration(0.25) == "250ms"


def test_fmt_duration_seconds_one_decimal():
    assert ex._fmt_duration(2.5) == "2.5s"


def test_fmt_duration_minutes():
    assert ex._fmt_duration(90) == "1m 30s"
    assert ex._fmt_duration(120) == "2m"


def test_fmt_duration_hours():
    assert ex._fmt_duration(3600) == "1h"
    assert ex._fmt_duration(3600 + 25 * 60) == "1h 25m"


# --- _read_plan_text (EXPLAIN → text) ---------------------------------------

def test_read_plan_text_single_column_joins_lines():
    rows = [("Aggregate  (cost=1.0..2.0)",), ("  ->  Seq Scan on t",)]
    text, n, trunc = ex._read_plan_text(iter(rows), ["QUERY PLAN"])
    assert text == "Aggregate  (cost=1.0..2.0)\n  ->  Seq Scan on t"
    assert n == 2
    assert trunc is False


def test_read_plan_text_truncates_at_budget(monkeypatch):
    # Tiny budget: the second line pushes past it and is dropped.
    monkeypatch.setattr(ex.cfg, "get_int", lambda key, default=0: 20)
    rows = [("x" * 10,), ("y" * 10,), ("z" * 10,)]
    text, n, trunc = ex._read_plan_text(iter(rows), ["QUERY PLAN"])
    assert n == 1
    assert text == "x" * 10
    assert trunc is True


# --- _plan_code_blocks (text → fenced Slack sections) -----------------------

def _all_within_slack_limit(blocks):
    return all(len(b["text"]["text"]) <= 3000 for b in blocks)


def test_plan_code_blocks_short_is_one_fenced_block():
    blocks = ex._plan_code_blocks("line1\nline2")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "section"
    assert blocks[0]["text"]["text"] == "```\nline1\nline2\n```"


def test_plan_code_blocks_empty_plan():
    blocks = ex._plan_code_blocks("")
    assert len(blocks) == 1
    assert "(empty plan)" in blocks[0]["text"]["text"]


def test_plan_code_blocks_splits_long_plan_on_lines():
    plan = "\n".join(f"line-{i:04d}-{'x' * 40}" for i in range(200))
    blocks = ex._plan_code_blocks(plan)
    assert len(blocks) >= 2
    assert _all_within_slack_limit(blocks)
    # No line is lost or split across blocks (each chunk ends cleanly).
    rejoined = "\n".join(
        b["text"]["text"].removeprefix("```\n").removesuffix("\n```")
        for b in blocks)
    assert rejoined == plan


def test_plan_code_blocks_hard_slices_overlong_single_line():
    plan = "z" * 6000  # one line longer than a single Slack section
    blocks = ex._plan_code_blocks(plan)
    assert len(blocks) >= 2
    assert _all_within_slack_limit(blocks)


# --- the classifier reads code, not prose ----------------------------------
#
# These words are ordinary vocabulary in a comment ABOUT an index build, and
# the guard used to match the raw statement text. Request 4483 — entirely
# read-only — was escalated to manual DBA execution because a note on it read
# "CONCURRENTLY'siz unique index", Turkish for "a unique index WITHOUT
# CONCURRENTLY". The sentence denying the keyword is what tripped on it.

def test_concurrently_in_a_line_comment_is_not_incompatible():
    sql = ("-- reservation.booking_items uzerinde CONCURRENTLY'siz unique index\n"
           "SELECT count(*) FROM reservation.booking_items")
    assert not ex._is_txn_incompatible(sql)


def test_concurrently_in_a_block_comment_is_not_incompatible():
    assert not ex._is_txn_incompatible("/* build it CONCURRENTLY later */ SELECT 1")


def test_vacuum_in_a_comment_is_not_incompatible():
    assert not ex._is_txn_incompatible("-- no VACUUM in this one\nSELECT 1")


def test_keyword_inside_a_string_literal_is_not_incompatible():
    assert not ex._is_txn_incompatible("SELECT 'run it CONCURRENTLY' AS note")
    assert not ex._is_txn_incompatible("SELECT 'ALTER SYSTEM is banned' AS note")


def test_a_commented_query_still_catches_the_real_statement():
    """Stripping comments must not stop the guard seeing actual code."""
    sql = ("-- this one really does need it\n"
           "CREATE INDEX CONCURRENTLY ix ON t (a)")
    assert ex._is_txn_incompatible(sql)
