"""The super-admin path: almost nothing refused, two things not negotiable.

A super-admin already holds unrestricted access to these databases through a
desktop SQL client. Routing that work through QueryHub buys an audit trail, not
a restriction — so `unrestricted=True` stops refusing the bulk-destructive
statements and asks instead.

Two exceptions, and the tests come in pairs so neither can quietly widen:

  1. A statement that changes the logging settings is refused in BOTH modes. The
     audit trail is the entire reason this path exists; a statement that turns it
     off would make the guarantee voluntary.
  2. Ordinary statements must produce NO confirmation. A prompt that fires on
     everything is a prompt nobody reads, which is worse than no prompt.

And the default path must be byte-identical, because ~1500 existing tests and
every non-admin user depend on it.
"""
from __future__ import annotations

import pytest

from dba_slack_bot import query_safety as qs


# ---------------------------------------------------------------------------
# 1. the audit trail is not negotiable — in either mode
# ---------------------------------------------------------------------------

AUDIT_KILLERS = [
    "ALTER DATABASE reward_service SET log_statement = 'none'",
    "ALTER SYSTEM SET log_min_duration_statement = -1",
    "ALTER SYSTEM SET log_destination = 'stderr'",
    "ALTER ROLE app_user SET pgaudit.log = 'none'",
    "ALTER USER app_user IN DATABASE x SET log_statement = 'none'",
    "ALTER SYSTEM RESET ALL",
    "ALTER DATABASE reward_service RESET ALL",
]


@pytest.mark.parametrize("sql", AUDIT_KILLERS)
@pytest.mark.parametrize("unrestricted", [False, True])
def test_changing_a_logging_setting_is_always_blocked(sql, unrestricted):
    r = qs.analyze(sql, unrestricted=unrestricted)
    assert r.blocked, f"not blocked with unrestricted={unrestricted}: {sql}"
    assert not r.confirmations, "an audit-killer must never be merely confirmed"
    assert "logging" in r.blockers[0]


@pytest.mark.parametrize("unrestricted", [False, True])
def test_a_normal_alter_is_not_mistaken_for_one(unrestricted):
    """The guard reads raw text, so over-matching is the risk to pin."""
    for sql in ("ALTER TABLE logs ADD COLUMN note text",
                "ALTER TABLE rewards SET (fillfactor = 70)",
                "ALTER DATABASE reward_service SET work_mem = '64MB'",
                "SELECT * FROM login_attempts"):
        r = qs.analyze(sql, unrestricted=unrestricted)
        assert not any("logging" in b for b in r.blockers), sql


# ---------------------------------------------------------------------------
# 2. bulk-destructive: refused by default, confirmed for a super-admin
# ---------------------------------------------------------------------------

WHERELESS = [
    "UPDATE rewards SET x = 1",
    "DELETE FROM rewards",
    "UPDATE rewards SET x = 1 WHERE 1=1",
    "DELETE FROM rewards WHERE TRUE",
]


@pytest.mark.parametrize("sql", WHERELESS)
def test_default_mode_still_refuses(sql):
    r = qs.analyze(sql)
    assert r.blocked
    assert not r.confirmations, "the default path must not gain a confirm route"
    # The exact wording is asserted by older tests; keep the shape.
    assert "is blocked" in r.blockers[0]


@pytest.mark.parametrize("sql", WHERELESS)
def test_unrestricted_asks_instead_of_refusing(sql):
    r = qs.analyze(sql, unrestricted=True)
    assert not r.blocked, f"a super-admin should not be refused: {sql}"
    assert r.needs_confirmation
    assert "every row" in r.confirmations[0]


IRREVERSIBLE = ["DROP TABLE rewards", "TRUNCATE rewards",
                "DROP SCHEMA public CASCADE", "DROP DATABASE reward_service",
                "ALTER TABLE rewards DROP COLUMN note",
                "ALTER TABLE rewards DROP CONSTRAINT rewards_pkey"]


@pytest.mark.parametrize("sql", IRREVERSIBLE)
def test_irreversible_statements_ask_first(sql):
    r = qs.analyze(sql, unrestricted=True)
    assert not r.blocked
    assert r.needs_confirmation, f"no confirmation for: {sql}"


@pytest.mark.parametrize("sql", IRREVERSIBLE)
def test_they_were_never_blocked_to_begin_with(sql):
    """Measured before this change: DROP/TRUNCATE pass the safety layer and are
    gated by the ddl grant alone. If that ever changes, the confirm route above
    is testing something else."""
    r = qs.analyze(sql)
    assert not r.blocked
    assert not r.confirmations


# ---------------------------------------------------------------------------
# 3. the prompt has to stay rare
# ---------------------------------------------------------------------------

ORDINARY = [
    "SELECT 1",
    "SELECT * FROM rewards WHERE id = 5",
    "UPDATE rewards SET x = 1 WHERE id = 5",
    "DELETE FROM rewards WHERE id = 5",
    "INSERT INTO rewards (id) VALUES (1)",
    "CREATE TABLE t (id int)",
    "ALTER TABLE rewards ADD COLUMN note text",
    "CREATE INDEX idx_x ON rewards (id)",
    "VACUUM rewards",
]


@pytest.mark.parametrize("sql", ORDINARY)
def test_ordinary_work_is_never_confirmed(sql):
    r = qs.analyze(sql, unrestricted=True)
    assert not r.blocked, sql
    assert not r.confirmations, (
        f"confirmation on ordinary work ({sql}) — a prompt that fires on "
        f"everything is one nobody reads")


# ---------------------------------------------------------------------------
# 4. the flag's own semantics
# ---------------------------------------------------------------------------

def test_a_blocker_is_not_clickable_past():
    """needs_confirmation must be False whenever anything blocks: otherwise a
    caller that only checks needs_confirmation would run a blocked statement."""
    r = qs.analyze("ALTER SYSTEM SET log_statement = 'none'", unrestricted=True)
    assert r.blocked
    assert not r.needs_confirmation


def test_unrestricted_does_not_change_the_tier():
    """The tier decides which credential connects. If unrestricted silently
    reclassified, the wrong credential would run the statement."""
    for sql in ("SELECT 1", "UPDATE rewards SET x=1 WHERE id=5",
                "DROP TABLE rewards", "CREATE TABLE t (id int)"):
        assert (qs.analyze(sql).main_tier
                == qs.analyze(sql, unrestricted=True).main_tier), sql


def test_the_default_is_restricted():
    """A caller that forgets the argument must get the safe behaviour."""
    assert qs.analyze("UPDATE rewards SET x = 1").blocked


def test_every_confirmation_says_what_it_costs():
    """A confirmation the reader cannot act on is a click-through."""
    for sql in WHERELESS + IRREVERSIBLE:
        r = qs.analyze(sql, unrestricted=True)
        for c in r.confirmations:
            assert len(c) > 30, f"too thin to be read: {c!r}"
            assert any(w in c for w in ("undone", "every row", "data")), c
