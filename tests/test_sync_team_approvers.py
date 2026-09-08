"""Making each team's lead an approver for what that team owns.

The rule the pod fleet needs is BOTH conditions: the request comes from my
team AND it is for one of my team's databases. `can_approve` expresses that as
a scoped team plus a scoped target — and `scope_target_id` holds one target,
so a team owning seven databases is seven rows. Twenty-three rows for five
leads is not a shape anyone maintains by hand, which is why this script
exists and why what it refuses to do matters more than what it writes.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "scripts" / "sync_team_approvers.py").read_text(encoding="utf-8")
MIG = (ROOT / "migrations"
       / "114_role_assignment_source.sql").read_text(encoding="utf-8")
ADMIN_API = (ROOT / "src" / "queryhub" / "web"
             / "routes_admin.py").read_text(encoding="utf-8")


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sta = _load("sync_team_approvers")
# The CSV moved here when ownership became a relation: the approver sync reads
# `target_team`, and this is what fills it.
sto = _load("sync_target_owners")


# --- the CSV that records ownership -----------------------------------------


def test_both_columns_are_required_and_named(tmp_path):
    p = tmp_path / "o.csv"
    p.write_text("team,server\na,b\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        sto.read_csv(p)
    assert "target" in str(e.value)


def test_a_team_owning_several_targets_is_several_rows(tmp_path):
    p = tmp_path / "o.csv"
    p.write_text("team,target\nt1,a\nt1,b\nt2,c\n", encoding="utf-8")
    pairs, bad = sto.read_csv(p)
    assert sorted(pairs) == [("t1", "a"), ("t1", "b"), ("t2", "c")]
    assert bad == []


def test_a_half_filled_row_is_reported_not_guessed(tmp_path):
    p = tmp_path / "o.csv"
    p.write_text("team,target\nt1,\n,x\nt2,c\n", encoding="utf-8")
    pairs, bad = sto.read_csv(p)
    assert pairs == [("t2", "c")] and len(bad) == 2


# --- what it refuses to do ---------------------------------------------------


def test_it_never_grants_access():
    """An approver role decides other people's requests and reaches no
    database. A lead who needs to query what they approve gets a grant from a
    person, separately."""
    assert "INSERT INTO access_grant" not in SRC
    assert "INSERT INTO role_assignment" in SRC
    assert "'approver'" in SRC


def test_it_only_touches_rows_carrying_its_own_source():
    """A role somebody wrote by hand, and one the migration-109 mirror
    projects, are both invisible to it."""
    assert "WHERE source = %s AND role = 'approver'" in SRC
    assert "source" in MIG and "ADD COLUMN IF NOT EXISTS source" in MIG


def test_source_is_not_folded_into_mirrored_from():
    """They mean different things and have different owners. Collapsing them
    would let the mirror revoke a role an org sync wrote."""
    assert "mirrored_from" in MIG          # named, to say it is NOT that
    assert "ALTER TABLE role_assignment ADD COLUMN IF NOT EXISTS source" in MIG


def test_an_empty_source_is_refused():
    """It is the only thing separating this script's rows from everyone
    else's; an empty one would make every hand-written role fair game."""
    assert "--source names the rows this owns" in SRC


def test_a_team_with_no_lead_is_reported_not_invented():
    """Being named a lead in an org chart is not a decision to give somebody
    approval authority over production."""
    assert "owns targets but has no " in SRC


def test_a_disabled_lead_gets_nothing():
    # The sentence is split across f-string lines; match the half that is not.
    assert "disabled — skipped" in SRC
    assert 'if not lead["enabled"]' in SRC


def test_the_tier_is_a_ceiling_with_a_read_only_default():
    assert 'default="ro"' in SRC and "choices=_TIERS" in SRC


def test_a_bulk_run_does_not_dm_by_default():
    """Twenty-three roles written at once would be twenty-three messages
    about authority that decides nothing until the model switch is on."""
    assert "app.auth_dm_suppress" in SRC and '"--notify"' in SRC


# --- reconciling -------------------------------------------------------------


def test_a_changed_ceiling_is_revoke_and_recreate():
    """A role is immutable, and `role_assignment_live_uq` would refuse the
    same scope twice anyway."""
    assert "retier" in SRC
    i = SRC.index('for key in p["drop"] + p["retier"]:')
    assert "revoked_at = NOW()" in SRC[i:i + 220]


def test_a_target_the_team_no_longer_owns_is_revoked():
    assert 'p["drop"]' in SRC and "no longer owned by that team" in SRC


def test_a_new_team_member_needs_no_row_at_all():
    """The role is scoped to the TEAM, so a new member is covered the moment
    they appear in `team_member`. Only a new TARGET, or a change of lead,
    needs this script to run — and saying so is what stops someone wiring a
    membership hook that does nothing."""
    assert "A new member of a team needs nothing" in SRC


# --- the screen has to tell the three origins apart -------------------------


def test_a_synced_role_cannot_be_revoked_from_the_screen():
    """It would come back on the next run — a change that undoes itself, the
    same reason a mirrored row is refused."""
    assert 'if row["source"]:' in ADMIN_API
    assert "would come back on the next run" in ADMIN_API


def test_the_api_names_all_three_origins():
    assert '"synced" if r.get("source")' in ADMIN_API
    assert '"syncedFrom"' in ADMIN_API


# --- ownership became a relation (migration 115) ----------------------------

OWNERS = (ROOT / "scripts" / "sync_target_owners.py").read_text(encoding="utf-8")
MIG115 = (ROOT / "migrations" / "115_target_team.sql").read_text(encoding="utf-8")


def test_ownership_is_many_to_many_because_the_data_is():
    """Six production databases are claimed by two teams each — services owned
    by different teams sharing one RDS. A nullable `owner_team_id` would have
    forced a silent choice between them, on the row that decides who may
    approve access to that database."""
    assert "CREATE TABLE IF NOT EXISTS target_team" in MIG115
    assert "PRIMARY KEY (target_id, team_id)" in MIG115
    assert "owner_team_id" not in MIG115.replace("`target_servers.owner_team_id`", "")


def test_ownership_grants_nothing():
    """It says who is responsible for a database. What anyone may read stays
    in access_grant, decided by a person."""
    assert "INSERT INTO access_grant" not in OWNERS
    assert "Ownership is NOT access" in MIG115


def test_a_hand_made_link_survives_every_sync():
    """The half that makes "not mandatory, but possible" true: a target the
    portal has never heard of — 20 of the enabled fleet — can still be linked,
    and no sync will take it away."""
    assert "source" in MIG115
    assert "AND source = %s" in OWNERS      # deletes are scoped to its own rows


def test_the_two_foreign_keys_are_deliberately_asymmetric():
    """Ownership is metadata about a target, so retiring the target takes it
    along; a team that still owns something is not one to remove unnoticed."""
    assert "REFERENCES target_servers (id) ON DELETE CASCADE" in MIG115
    assert "REFERENCES team (id)           ON DELETE RESTRICT" in MIG115


def test_the_approver_sync_reads_the_model_not_a_csv():
    """It used to take ownership as a CSV. Migration 115 made it a relation,
    so the answer lives where a screen can show it and a person can correct
    it — and the sync needs no input beyond which source it owns."""
    assert "FROM target_team tt" in SRC
    assert "--csv" not in SRC


def test_the_connections_screen_can_finally_say_whose_database_it_is():
    """The question an admin brings to that page before deciding anything
    else, and it could not be answered while the link was a runtime string
    join on the hostname."""
    assert '"owners"' in ADMIN_API
    assert "FROM target_team tt" in ADMIN_API
