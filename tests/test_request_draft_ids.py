"""Reserved request ids: the number a query tab shows before anything is submitted.

The requirement, in the operator's words: opening a query screen should create a
row in the bot's own `requests` table and put that PK on screen, so the id is
known from the start rather than appearing after submit — the way SSMS shows a
session id in the tab.

That means putting rows in the table this product treats as its audit trail, so
the shape matters:

  * a draft is `status = 'draft'`, with no target, no database and no SQL.
    Migration 086 relaxed `target_server_id` (it is a foreign key, so there was
    no sentinel available) and added a CHECK that keeps every OTHER status
    complete.
  * `requests_reportable` excludes drafts. Measured before relying on it: all 19
    views that read requests go through that one view, so the dashboard, the
    volume counts and the SLA figures cannot count somebody's open browser tabs.
  * submitting CLAIMS the reserved id: the draft is deleted and the real row is
    inserted with that id inside the same transaction. Delete-and-reinsert
    rather than UPDATE, because the two INSERTs in create_request carry the
    rate-limit recheck, the auto-approve decision and the audit rows, and a
    third near-copy of that SQL is how the three drift apart.
  * claiming FAILS OPEN. A draft that expired, was reaped, or belongs to
    somebody else gives up the pretty number and the submission proceeds with a
    fresh one. Losing a convenience must never lose a real query.
"""
import pytest

from queryhub import core_submit


class FakeCur:
    """Records SQL and answers the two questions the draft code asks."""

    def __init__(self, *, open_drafts=0, delete_rows=1, new_id=4242):
        self.open_drafts = open_drafts
        self.delete_rows = delete_rows
        self.new_id = new_id
        self.sql: list[tuple[str, tuple]] = []
        self.rowcount = 0
        self._next = None

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params or ()))
        low = sql.lower()
        if "count(*)" in low:
            self._next = {"n": self.open_drafts}
            self.rowcount = 1
        elif low.strip().startswith("delete"):
            self.rowcount = self.delete_rows
            self._next = None
        elif "insert into requests" in low:
            self._next = {"id": self.new_id}
            self.rowcount = 1

    def fetchone(self):
        return self._next

    def joined(self):
        return " | ".join(s for s, _ in self.sql).lower()


@pytest.fixture
def fake_txn(monkeypatch):
    """db.transaction() yielding a FakeCur we can inspect."""
    import contextlib
    holder = {}

    def make(cur):
        @contextlib.contextmanager
        def txn():
            yield cur
        monkeypatch.setattr(core_submit.db, "transaction", txn)
        holder["cur"] = cur
        return cur
    return make


# ------------------------------------------------------------------ reserving


def test_reserving_writes_a_draft_with_no_target_and_no_sql(fake_txn):
    cur = fake_txn(FakeCur(new_id=1994))
    assert core_submit.reserve_request_id("local:alice") == 1994
    ins = [s for s, _ in cur.sql if "insert into requests" in s.lower()]
    assert len(ins) == 1
    sql = ins[0].lower()
    assert "'draft'" in sql
    assert "null" in sql, "target_server_id must be NULL, not a sentinel id"


def test_a_reserved_row_belongs_to_the_caller(fake_txn):
    cur = fake_txn(FakeCur())
    core_submit.reserve_request_id("local:bob")
    params = [p for s, p in cur.sql if "insert into requests" in s.lower()][0]
    assert "local:bob" in params


def test_hitting_the_cap_reaps_instead_of_refusing(fake_txn, monkeypatch):
    """Refusing would mean "you cannot open a new query tab", which is a much
    worse failure than dropping the oldest abandoned reservation."""
    monkeypatch.setattr(core_submit.cfg, "get_int", lambda k, d=None: 50)
    cur = fake_txn(FakeCur(open_drafts=50))
    assert core_submit.reserve_request_id("local:carol") > 0
    assert "delete from requests" in cur.joined()


def test_under_the_cap_nothing_is_deleted(fake_txn, monkeypatch):
    monkeypatch.setattr(core_submit.cfg, "get_int", lambda k, d=None: 50)
    cur = fake_txn(FakeCur(open_drafts=3))
    core_submit.reserve_request_id("local:dave")
    assert "delete from requests" not in cur.joined()


# ------------------------------------------------------------------- claiming


def test_claiming_returns_the_reserved_id():
    cur = FakeCur(delete_rows=1)
    assert core_submit._claim_draft(cur, 1994, "local:alice") == 1994


def test_claiming_scopes_the_delete_to_the_owner_and_to_drafts():
    """Without both predicates this would be a way to delete somebody else's
    request by guessing an id."""
    cur = FakeCur(delete_rows=1)
    core_submit._claim_draft(cur, 1994, "local:alice")
    sql, params = cur.sql[0]
    assert "status = 'draft'" in sql
    assert "requester_slack_id = %s" in sql
    assert params == (1994, "local:alice")


def test_a_draft_that_is_gone_gives_up_the_id_rather_than_failing():
    """Fail-open: reaped, expired, or someone else's. The submission continues."""
    cur = FakeCur(delete_rows=0)
    assert core_submit._claim_draft(cur, 1994, "local:alice") is None


def test_no_draft_id_means_no_query_at_all():
    cur = FakeCur()
    assert core_submit._claim_draft(cur, None, "local:alice") is None
    assert cur.sql == [], "spent a DELETE on nothing"


# -------------------------------------------------------------------- reaping


def test_reaping_uses_the_configured_ttl(fake_txn, monkeypatch):
    monkeypatch.setattr(core_submit.cfg, "get_int", lambda k, d=None: 24)
    cur = fake_txn(FakeCur(delete_rows=7))
    assert core_submit.reap_stale_drafts() == 7
    sql, params = cur.sql[0]
    assert "status = 'draft'" in sql and "make_interval" in sql
    assert params == (24,)


def test_a_zero_ttl_disables_reaping(monkeypatch):
    monkeypatch.setattr(core_submit.cfg, "get_int", lambda k, d=None: 0)
    called = []
    monkeypatch.setattr(core_submit.db, "transaction",
                        lambda: called.append(1))
    assert core_submit.reap_stale_drafts() == 0
    assert not called, "opened a transaction just to delete nothing"


# ------------------------------------------------ drafts stay out of sight


def test_the_submit_path_can_be_given_a_draft_id():
    import inspect
    sig = inspect.signature(core_submit.create_request)
    assert "draft_id" in sig.parameters
    assert sig.parameters["draft_id"].default is None, \
        "adopting an id must be opt-in; the Slack path may not send one"


def test_both_insert_branches_can_carry_the_reserved_id():
    """create_request has two INSERTs (auto-approved and pending). A reserved id
    that only worked on one of them would silently renumber half the requests —
    and which half depends on the requester's grants."""
    import inspect
    src = inspect.getsource(core_submit.create_request)
    assert src.count("id_val + (") == 2
    assert src.count("{id_col}") == 2
    assert src.count("{id_ph}") == 2


def test_the_reaper_runs_from_the_scheduler_loop():
    """Not from cron: the vanilla profile runs no Slack cron jobs, and an install
    that never reaps grows a draft per abandoned tab for ever."""
    import inspect

    from queryhub import executor
    src = inspect.getsource(executor.scheduler_loop)
    assert "reap_stale_drafts()" in src


@pytest.mark.parametrize("module,needle", [
    ("queryhub.web.routes_data", "status <> 'draft'"),
    ("queryhub.web.routes_queries", "r.status <> 'draft'"),
    ("queryhub.web.ops_metrics", "status <> 'draft'"),
    ("queryhub.slack_app.subcommands", "r.status <> 'draft'"),
    ("queryhub.slack_app.handlers", "r.status <> 'draft'"),
])
def test_every_listing_surface_filters_drafts_out(module, needle):
    """A reserved id has no SQL and no target, so showing one in history, in the
    notification feed or in the reuse picker is showing an empty row."""
    import importlib
    import inspect
    src = inspect.getsource(importlib.import_module(module))
    assert needle in src, f"{module} does not exclude drafts"
