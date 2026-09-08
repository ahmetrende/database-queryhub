"""Keeping the new tables current, whoever does the writing.

`access_model_v2` switches reads to the nine-table model while every write path
still targets the old tables. Changing the ~25 write sites would not have been
enough: an operator granting access in psql is a documented, used path — that is
why migration 060 exists at all — and no amount of Python covers it. So the
projection is maintained where every writer has to pass, by AFTER triggers on
the old tables.

The triggers run against a real PostgreSQL, and were exercised there against the
production tables in a rolled-back transaction before being applied: a new
grant, a tier change, a revoke, an array of databases expanded into rows, the
bypass flag on and off, a team grant added and removed, and the notification
count. What is pinned here is the reasoning — the properties that make the
arrangement safe, each tied to what goes wrong without it.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MIG = (ROOT / "migrations" / "109_mirror_legacy_writes.sql").read_text(encoding="utf-8")
COPY = (ROOT / "scripts" / "copy_access_model.py").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    m = re.search(rf"CREATE OR REPLACE FUNCTION {name}\(.*?\n\$\$ LANGUAGE",
                  MIG, re.S)
    assert m, f"{name} is gone"
    return m.group(0)


# --- where the projection is maintained -------------------------------------


@pytest.mark.parametrize("table", [
    "requesters", "admins", "user_target_grants", "auto_approve_grants",
    "user_row_limit_overrides", "report_excluded_users", "team_members",
    "team_target_grants", "teams"])
def test_every_legacy_table_that_can_change_access_is_mirrored(table):
    """Look for the trigger, not the name: `teams` is attached by a literal
    CREATE TRIGGER while the rest come from a loop over quoted names, and a
    test that only knew one of those shapes would pass while a table went
    uncovered."""
    attached = (f"'{table}'" in MIG                       # in a loop's array
                or f"ON {table}\n" in MIG                 # literal statement
                or f"ON {table} " in MIG)
    assert attached, f"{table} can change access and is not mirrored"


def test_the_mirror_recomputes_a_subject_rather_than_translating_a_statement():
    """Idempotent by construction: a missed edge case shows up as a row
    corrected on the next write, not as permanent drift. It also means the same
    function serves the backfill and the ongoing sync, so the mapping has one
    implementation rather than two."""
    body = _fn("mirror_principal")
    assert "p_slack" in body
    assert "FROM user_target_grants" in body and "FROM auto_approve_grants" in body


def test_a_membership_change_re_syncs_the_person_as_well_as_the_team():
    """Joining a team moves that person's access, and the team's own rows do
    not change at all — syncing only the team would leave them behind."""
    body = _fn("mirror_by_team")
    assert "mirror_principal" in body and "team_members" in body


# --- the mirror owns exactly its own rows -----------------------------------


def test_mirrored_rows_are_marked_so_the_mirror_can_tell_them_apart():
    """It may revoke a row it no longer finds a source for. Without a marker
    that reach extends to rows a person wrote by hand."""
    assert "mirrored_from" in MIG
    for stmt in ("UPDATE access_grant g SET revoked_at",
                 "UPDATE role_assignment ra SET revoked_at",
                 "DELETE FROM principal_setting s"):
        i = MIG.index(stmt)
        assert "mirrored_from" in MIG[i:i + 400], f"{stmt} does not scope itself"


def test_a_grant_with_no_source_left_is_revoked_not_deleted():
    """A grant that existed is a fact, and somebody may be reading a message
    about it. Deleting the row deletes the answer to 'what did they have'."""
    body = _fn("mirror_principal")
    assert "UPDATE access_grant g SET revoked_at = NOW()" in body
    assert "DELETE FROM access_grant" not in MIG


def test_the_backfill_claims_what_the_copy_script_already_wrote():
    """Otherwise the mirror sees no rows of its own, writes a second set beside
    them, and the unique index refuses — or worse, does not."""
    assert "UPDATE access_grant SET mirrored_from" in MIG
    assert "carried from user_target_grants" in MIG


# --- nobody gets told twice --------------------------------------------------


def test_mirroring_does_not_notify_because_the_source_already_did():
    """The old table's trigger told the person what changed. The mirror writes
    the same fact into access_grant, whose trigger would tell them again."""
    assert "app.auth_mirror" in MIG
    i = MIG.index("app.auth_mirror")
    guard = MIG[i - 400:i + 200]
    for table in ("access_grant", "role_assignment", "principal"):
        assert table in guard


def test_the_suppression_names_the_new_tables_rather_than_everything():
    """Suppressing wholesale would silence the source table's own message too,
    and which of two triggers on one table fires first is not something to
    depend on."""
    body = MIG[MIG.index("CREATE OR REPLACE FUNCTION auth_event_capture"):]
    guard = body[body.index("app.auth_mirror") - 300:body.index("app.auth_mirror") + 100]
    assert "TG_TABLE_NAME IN" in guard


def test_the_two_suppressions_are_not_the_same_switch():
    """`auth_dm_suppress` is for an app path that sends its own richer DM;
    `auth_mirror` is for a projection that must not speak at all. Collapsing
    them would make the copy script silence the legacy triggers as well."""
    assert "app.auth_dm_suppress" in MIG and "app.auth_mirror" in MIG


# --- it can be switched off --------------------------------------------------


def test_the_mirror_can_be_turned_off_without_a_deploy():
    """It runs inside somebody's grant transaction. A bug in it must not be
    something only a release can stop."""
    assert "access_model_mirror" in MIG
    assert "CREATE OR REPLACE FUNCTION mirror_enabled" in MIG
    for name in ("mirror_principal", "mirror_team"):
        assert "mirror_enabled()" in _fn(name), name


def test_the_default_is_on_because_drift_is_the_worse_failure():
    """Writing rows nobody reads yet is harmless. The two models silently
    disagreeing is not."""
    assert "'access_model_mirror', 'on'" in MIG
    assert "TRUE)" in _fn("mirror_enabled")     # absent row reads as on


# --- and the drift gate still means something -------------------------------


def test_the_copy_script_still_gates_the_flag():
    """The mirror should make drift impossible, which is exactly why the check
    stays: it is what proves the mirror is working.

    It also had to STOP being a timestamp comparison for that to be true. The
    mirror writes inside the same transaction as the legacy row, so
    `granted_at` and the mirrored `created_at` land microseconds apart in an
    order nothing guarantees — the check the mirror made necessary was the
    check the mirror broke."""
    assert "differ between the two models" in COPY
    assert "max(created_at) FROM access_grant" not in COPY


def test_the_copy_script_and_the_mirror_agree_on_the_marker():
    """Both write the same rows. If they label them differently, each sees the
    other's work as foreign and neither will correct it."""
    assert "mirrored from " in MIG          # the mirror's reason prefix
    assert "carried from " in COPY          # the copy's
    assert "mirrored_from" in MIG
