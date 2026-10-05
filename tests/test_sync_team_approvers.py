"""Making each team's lead an approver for what that team owns.

The rule: the lead approves requests TO their team's databases, whoever sends
them, up to the ceiling. It used to require the request to come FROM the team
as well, which left every cross-pod grant with nobody but the admins to
approve it. `scope_target_id` holds one target, so a team owning seven
databases is seven rows. That is not a shape anyone maintains by hand, which
is why this script exists and why what it refuses to do matters more than what
it writes.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# The script is the CLI. The rules moved into the package when the Connections
# screen started to run the same reconcile for one target, so both are read.
SRC = ((ROOT / "scripts" / "sync_team_approvers.py").read_text(encoding="utf-8")
       + (ROOT / "src" / "queryhub"
          / "owner_approvers.py").read_text(encoding="utf-8"))
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


# --- any requester, one owned target -----------------------------------------


def test_a_row_names_no_team_so_any_requester_is_covered():
    """The wanted key carries no team, and the insert writes all_teams, so the
    requester's pod stops mattering."""
    assert 'key = (lead["principal_id"], None, row["target_id"])' in SRC
    assert "team_id is None" in SRC


def test_the_old_team_scoped_row_is_replaced_not_kept_beside_it():
    """A live row keyed on a team no longer matches a wanted key, so it lands in
    `drop` and is revoked in the same run that adds its any-requester twin."""
    assert "scoped to one team, replaced by the" in SRC
    assert "any-requester row above" in SRC


def _owner_row(target_id, max_tier="ro"):
    return {"role": "approver", "scope_team_id": None, "all_teams": True,
            "scope_target_id": target_id, "all_targets": False,
            "max_tier": max_tier, "any_tier": False}


def _request(requester="U0EXAMPLE02", target_id=13, tier="ro"):
    return {"requester_slack_id": requester, "target_server_id": target_id,
            "required_tier": tier}


@pytest.fixture
def owner_lead(monkeypatch):
    from queryhub import access
    monkeypatch.setattr(access, "roles", lambda pid: [_owner_row(13)])

    def _no_team_lookup(*a, **k):
        raise AssertionError("an any-requester row must not look up teams")
    monkeypatch.setattr(access.db, "fetch_all", _no_team_lookup)
    return access


def test_the_owner_lead_approves_a_read_from_another_pod(owner_lead):
    assert owner_lead.can_approve("U0EXAMPLE01", _request())


def test_the_ceiling_still_holds(owner_lead):
    assert not owner_lead.can_approve("U0EXAMPLE01", _request(tier="rw"))


def test_another_server_stays_out_of_scope(owner_lead):
    assert not owner_lead.can_approve("U0EXAMPLE01", _request(target_id=14))


def test_a_lead_never_approves_their_own_request(owner_lead):
    assert not owner_lead.can_approve("U0EXAMPLE01",
                                      _request(requester="U0EXAMPLE01"))


# --- owners from the Connections screen ----------------------------------------


def test_the_screen_and_the_script_own_the_same_rows():
    """A screen that wrote under another source could not revoke the script's
    row when an owner goes, and the unique index would refuse its own."""
    from queryhub import owner_approvers
    assert owner_approvers.SOURCE == "pod-sync"
    assert "owner_approvers.SOURCE" in ADMIN_API
    assert "owner_approvers.plan" in ADMIN_API and "owner_approvers.apply" in ADMIN_API


def test_an_owner_change_reconciles_only_its_target():
    assert "target_id=target_id" in ADMIN_API
    assert " AND scope_target_id = %s" in SRC
    assert " WHERE tt.target_id = %s " in SRC


def test_a_key_another_source_holds_is_skipped_not_inserted():
    # role_assignment_live_uq ignores the source: a second insert would fail.
    assert "def _held_elsewhere" in SRC and "source IS DISTINCT FROM %s" in SRC


def test_a_synced_owner_cannot_be_removed_from_the_screen():
    assert 'if own["source"]:' in ADMIN_API
    assert "run would add the owner again" in ADMIN_API
    assert "AND team_id = %s AND source IS NULL" in ADMIN_API


def test_every_owner_change_is_audited():
    assert '"target_owner_added"' in ADMIN_API
    assert '"target_owner_removed"' in ADMIN_API


def test_the_screen_ceiling_follows_the_fleet_run():
    assert "owner_approvers.ceiling(cur, source)" in ADMIN_API
    assert "def ceiling" in SRC
