"""A SELECT with no columns is refused at submit.

PostgreSQL accepts `SELECT FROM users` — an empty target list is legal, verified
against a live server, which returned 86 rows and 0 columns. So nothing upstream
rejects it and nothing downstream can rescue it.

Observed live on 2026-07-28: `select  from users;` was approved, ran a full scan
of a production table, wrote a CSV whose header line was empty, reported 5000
rows, and rendered in the grid as row numbers with no columns. The user read the
empty grid as the product being broken, which is the right reading — a tool that
answers a typo with a silent scan and an empty table has misled them.

Blocked rather than warned: there is no result it could produce that anyone
wants, so running it only spends a scan and an approval round to reach nothing.
"""
import pytest

from queryhub import query_safety


@pytest.mark.parametrize("sql", [
    "select  from users;",                          # the reported one
    "select from users",
    "SELECT FROM users",
    "with c as (select 1 x) select from c",          # empty terminal select
    "select from t union select from u",             # both branches
    "select id from a union select from b",          # one branch is enough
])
def test_a_select_with_no_columns_is_blocked(sql):
    report = query_safety.analyze(sql)
    assert report.blocked, f"{sql!r} was allowed"
    assert any("no columns" in b.lower() or "lists no columns" in b.lower()
               for b in report.blockers), report.blockers


@pytest.mark.parametrize("sql", [
    "select * from users",
    "select id, email from users",
    "select 1 from users",
    "select count(*) from users",
    "select u.* from users u join teams t on t.id = u.team_id",
    "with c as (select 1 x) select x from c",
    "table users",                    # not a Select node at all
    "values (1), (2)",                # nor this
    "update users set a = 1 where id = 5",
    "select coalesce(name, '-') as n from users",
])
def test_a_real_select_is_not_blocked_by_this_rule(sql):
    """The guard must not cost anyone a legitimate query — `*`, aliases,
    aggregates, qualified stars, CTEs, and the non-SELECT row sources."""
    report = query_safety.analyze(sql)
    empty_col_blockers = [b for b in report.blockers if "no columns" in b.lower()]
    assert not empty_col_blockers, f"{sql!r} -> {empty_col_blockers}"


def test_the_message_tells_the_user_what_to_do():
    """A blocker that only says "invalid" makes the user guess. This one names
    the cause and both fixes, because the trigger is a typo."""
    msg = query_safety.analyze("select from users").blockers[0]
    assert "no columns" in msg.lower()
    assert "*" in msg                      # one of the two ways out
    assert "columns you want" in msg.lower()
