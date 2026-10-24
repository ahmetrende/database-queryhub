"""A rule with no end is a permanent rule, to every reader.

The application refuses to make such a rule (`auto_approve_limits`), but a DBA
can insert one with SQL on purpose (docs/OPERATIONS.md §30), and rules from
before the limit exist. So the readers must keep treating `expires_at IS NULL`
(and `valid_until IS NULL` in `access_grant`) as "never ends". Nothing in the
readers changed with the limit. These tests pin that.

No database: the readers run on rows and on captured SQL.
"""
import inspect
from datetime import datetime, timedelta, timezone

from queryhub import auto_approve, teams
from queryhub.web import routes_admin as ra

NOW = datetime.now(timezone.utc)


def _row(**kw):
    row = {"id": 31, "slack_user_id": "U0EXAMPLE002", "max_tier": "ro",
           "target_server_id": 7, "database_name": None,
           "starts_at": NOW - timedelta(days=400), "expires_at": None,
           "reason": "set with SQL", "granted_by": "U0EXAMPLE001"}
    row.update(kw)
    return row


def test_a_row_with_no_end_decides_a_request():
    permanent = _row()
    assert auto_approve.effective_grant("U0EXAMPLE002", "ro", target_server_id=7,
                                        rows=[permanent]) is permanent
    # Not a waiver for a tier above its own, and not for another target.
    assert auto_approve.effective_grant("U0EXAMPLE002", "rw", target_server_id=7,
                                        rows=[permanent]) is None
    assert auto_approve.effective_grant("U0EXAMPLE002", "ro", target_server_id=8,
                                        rows=[permanent]) is None


def test_a_row_with_no_end_outranks_a_dated_row_of_the_same_tier():
    dated = _row(id=32, expires_at=NOW + timedelta(days=30))
    permanent = _row(id=33)
    for rows in ([dated, permanent], [permanent, dated]):
        got = auto_approve.effective_grant("U0EXAMPLE002", "ro", target_server_id=7, rows=rows)
        assert got["id"] == 33


def test_a_row_with_no_end_is_read_by_the_active_query(monkeypatch):
    seen = {}

    def fetch_all(sql, params=None):
        seen["sql"], seen["params"] = sql, params
        return [_row()]

    monkeypatch.setattr(auto_approve.db, "fetch_all", fetch_all)
    monkeypatch.setattr(teams, "use_v2", lambda: False)
    [row] = auto_approve.active_grants("U0EXAMPLE002")
    assert row["expires_at"] is None
    assert "expires_at IS NULL OR expires_at > %s" in " ".join(seen["sql"].split())


def test_a_waiver_in_access_grant_with_no_end_is_read_too(monkeypatch):
    seen = {}
    monkeypatch.setattr(auto_approve.db, "fetch_all",
                        lambda sql, params=None: seen.setdefault("sql", sql) and [])
    auto_approve._v2_only_waivers("U0EXAMPLE002", NOW)
    assert "g.valid_until IS NULL OR g.valid_until > %(at)s" in " ".join(seen["sql"].split())


def test_the_screens_and_the_messages_say_no_end_instead_of_failing():
    assert auto_approve.fmt_until(None) == "no expiry"
    label = auto_approve.decided_by_name_for(_row())
    assert "no expiry" in label and "grant #31" in label
    # The Auto-approve list keeps a rule with no end, and the copy path skips none.
    assert "g.expires_at IS NULL OR g.expires_at > NOW()" in " ".join(
        inspect.getsource(ra.admin_auto_grants).split())
