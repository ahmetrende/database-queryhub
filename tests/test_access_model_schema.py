"""What the new access schema refuses.

Migration 105 moves authorization onto nine tables, and most of the safety in
that design is in the constraints rather than in the code above them: exactly
one subject per grant, a wildcard that cannot disagree with the column it
stands for, an admin role that cannot be partial. Those are the rules a future
edit could relax without anybody noticing until a grant means something other
than what it says — so each one is pinned to a statement that must be rejected.

The tests read the migration file. They do not need a database: a rule that is
not written down is not enforced, and a rule that is written down is checked
against a real PostgreSQL by scripts/apply_migrations.py and by the probe run
before it was applied. What this file guards is the wording surviving.
"""
import re
from pathlib import Path

import pytest

MIG = (Path(__file__).resolve().parent.parent / "migrations"
       / "105_access_model.sql").read_text(encoding="utf-8")

TABLES = ["tier", "principal", "principal_identity", "principal_credential",
          "team", "team_member", "access_grant", "role_assignment",
          "principal_setting"]


def _table(name: str) -> str:
    """The CREATE TABLE body for one table."""
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {name} \((.*?)\n\);", MIG, re.S)
    assert m, f"{name} is not created in the migration"
    return m.group(1)


@pytest.mark.parametrize("name", TABLES)
def test_every_table_is_created_idempotently(name):
    assert f"CREATE TABLE IF NOT EXISTS {name} (" in MIG


@pytest.mark.parametrize("name", TABLES)
def test_every_table_carries_the_standard_columns(name):
    body = _table(name)
    for col in ("created_at", "updated_at"):
        assert col in body, f"{name} has no {col}"
    if name != "tier":                       # a five-row vocabulary, not data
        for col in ("is_deleted", "deleted_at", "created_by"):
            assert col in body, f"{name} has no {col}"


@pytest.mark.parametrize("name", TABLES)
def test_every_table_gets_the_updated_at_trigger(name):
    """One trigger function, nine tables. A hand-maintained updated_at is wrong
    exactly when it matters: the row somebody changed outside the usual path."""
    block = MIG[MIG.index("FOREACH t IN ARRAY"):]
    assert f"'{name}'" in block


def test_a_grant_has_exactly_one_subject():
    assert "CHECK ((principal_id IS NULL) <> (team_id IS NULL))" in _table("access_grant")


def test_a_wildcard_cannot_disagree_with_its_column():
    """`all_targets` and `target_id` are two spellings of one fact. Letting
    them drift is how NULL came to mean both 'everything' and 'nothing'."""
    body = _table("access_grant")
    assert "CHECK (all_targets = (target_id IS NULL))" in body
    assert "CHECK (all_databases = (database_name IS NULL))" in body
    role = _table("role_assignment")
    assert "CHECK (all_teams = (scope_team_id IS NULL))" in role
    assert "CHECK (all_targets = (scope_target_id IS NULL))" in role
    assert "CHECK (any_tier = (max_tier IS NULL))" in role


def test_a_database_name_requires_a_target():
    """Naming a database while covering every target would describe a name on
    servers nobody checked."""
    assert "CHECK (all_databases OR NOT all_targets)" in _table("access_grant")


def test_merge_with_team_belongs_to_a_principals_own_row():
    assert "CHECK (NOT merge_with_team OR principal_id IS NOT NULL)" in _table("access_grant")


def test_merge_with_team_defaults_to_todays_behaviour():
    """False = ignore the team's rows, which is what user_target_grants does
    today. A migration that flipped this default would widen access silently."""
    assert re.search(r"merge_with_team\s+BOOLEAN\s+NOT NULL DEFAULT FALSE", _table("access_grant"))


def test_expiry_and_revocation_stay_separate():
    """Different endings with different consequences: an expired principal row
    must not fall through to the team, a revoked one does."""
    body = _table("access_grant")
    for col in ("valid_from", "valid_until", "revoked_at", "revoked_by"):
        assert col in body
    assert "CHECK (valid_until IS NULL OR valid_until > valid_from)" in body


def test_the_admin_role_is_never_partial():
    """`admin` is the one role that carries access, so a scoped one would be a
    half-super-admin nobody can reason about."""
    assert ("CHECK (role <> 'admin' OR (all_teams AND all_targets AND any_tier))"
            in _table("role_assignment"))


def test_the_roles_are_the_four_agreed_ones():
    assert "role IN ('approver', 'granter', 'importer', 'admin')" in _table("role_assignment")


def test_the_tier_vocabulary_is_data_not_a_check_constraint():
    """A CHECK would hard-code three tiers into every table that mentions one,
    so another deployment could not add a fourth without a migration."""
    assert "REFERENCES tier (name)" in _table("access_grant")
    assert "REFERENCES tier (name)" in _table("role_assignment")
    assert not re.search(r"tier\s+TEXT[^,]*CHECK\s*\(\s*tier\s+IN", MIG)
    for name in ("ro", "rw", "ddl"):
        assert f"('{name}'" in MIG


def test_uniqueness_is_partial_so_a_deleted_row_does_not_block_a_new_one():
    for idx in ("principal_email_uq", "principal_identity_uq", "team_name_uq",
                "team_member_uq", "principal_setting_uq"):
        m = re.search(rf"CREATE UNIQUE INDEX IF NOT EXISTS {idx}(.*?);", MIG, re.S)
        assert m, idx
        assert "WHERE" in m.group(1) and "is_deleted" in m.group(1), idx


def test_the_live_grant_index_treats_nulls_as_equal():
    """The wildcard columns are NULL. Under the default NULLS DISTINCT two
    identical fleet-wide grants would both be allowed."""
    m = re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS access_grant_live_uq(.*?);", MIG, re.S)
    assert m and "NULLS NOT DISTINCT" in m.group(1)
    assert "revoked_at IS NULL" in m.group(1)


def test_deleting_a_principal_cannot_delete_the_history():
    """Grants and roles are the record of who could do what. CASCADE there
    would answer 'who had access last March' with silence."""
    for name in ("access_grant", "role_assignment", "team_member",
                 "principal_identity", "principal_credential", "principal_setting"):
        body = _table(name)
        assert "ON DELETE CASCADE" not in body, f"{name} cascades"
        assert "ON DELETE RESTRICT" in body, f"{name} does not restrict"


def test_a_principal_is_off_until_somebody_turns_it_on():
    """`enabled` is the whitelist, and a directory sync may create rows."""
    assert re.search(r"enabled\s+BOOLEAN\s+NOT NULL DEFAULT FALSE", _table("principal"))


def test_a_person_can_hold_several_identities():
    body = _table("principal_identity")
    assert "principal_id" in body and "provider" in body and "external_id" in body
    assert "provider" not in _table("principal")


def test_requests_records_which_rule_allowed_it():
    """5564 requests on record and not one says which grant permitted it."""
    assert "ALTER TABLE requests" in MIG
    for col in ("access_grant_id", "role_assignment_id", "approved_by_principal_id"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in MIG


def test_the_alter_is_catalogue_only():
    """Nullable and defaultless: on the busiest table, a metadata-only change.
    A DEFAULT or a NOT NULL here would rewrite the table under a lock."""
    tail = MIG[MIG.index("ALTER TABLE requests"):]
    assert "DEFAULT" not in tail and "NOT NULL" not in tail


def test_the_migration_alters_nothing_else():
    """Purely additive is the reason this can be applied to a live system with
    nothing announced: no existing table changes, no data moves, no drops."""
    altered = set(re.findall(r"ALTER TABLE (\w+)", MIG))
    assert altered == {"requests"}, altered
    assert not re.search(r"\bDROP TABLE\b|\bDROP COLUMN\b|\bTRUNCATE\b", MIG)
    assert not re.search(r"^\s*(UPDATE|DELETE FROM)\s", MIG, re.M)
