"""Authorization changes on the new tables still reach the person.

Migration 060 put triggers on every table that could change what somebody may
do, so that a change made in psql notifies its subject just as an app path
does. The nine-table model would have arrived without that cover, and the day
the resolver switched over would have been the day authorization started
changing silently — the exact failure 060 exists to prevent.

The trigger side is checked against a real PostgreSQL when the migration is
applied. What is checked here is the half that decides what the person reads:
one message per kind of change, addressed to the right people.
"""
import pytest

from queryhub import auth_events as ae


def ev(table, op, new=None, old=None, user="U1", team_id=None):
    return {"table_name": table, "op": op, "slack_user_id": user,
            "team_id": team_id, "old_row": old, "new_row": new}


def grant(**over):
    row = {"target_id": 5, "all_targets": False, "database_name": None,
           "all_databases": True, "tier": "rw", "auto_approve": False,
           "merge_with_team": False, "team_id": None, "valid_until": None,
           "revoked_at": None}
    row.update(over)
    return row


ALIAS = {5: "orders-db"}


def notify(event, members=("U1", "U2"), team_name="platform-team"):
    return ae.build_notifications(
        event,
        alias_of=lambda t: ALIAS.get(t),
        team_info=lambda tid, table=None: (team_name, list(members)))


# --- a grant ----------------------------------------------------------------


def test_a_new_grant_names_the_tier_and_the_target():
    (who, text), = notify(ev("access_grant", "INSERT", grant()))
    assert who == "U1"
    assert "*RW*" in text and "`orders-db`" in text


def test_a_grant_on_every_target_is_prose_not_an_alias():
    """A backticked "every target" reads as a server somebody could go looking
    for."""
    (_, text), = notify(ev("access_grant", "INSERT",
                           grant(target_id=None, all_targets=True, tier="ddl")))
    assert "*every target*" in text and "`every target`" not in text


def test_a_grant_scoped_to_one_database_says_which():
    (_, text), = notify(ev("access_grant", "INSERT",
                           grant(all_databases=False, database_name="app")))
    assert "`app`" in text


def test_a_born_revoked_row_announces_nothing():
    """The copy writes history as it stands, including grants that had already
    been revoked. Nothing was granted, so there is nothing to say."""
    assert notify(ev("access_grant", "INSERT",
                     grant(revoked_at="2026-01-01"))) == []


def test_revoking_a_grant_says_so():
    (_, text), = notify(ev("access_grant", "UPDATE",
                           new=grant(revoked_at="2026-09-07"), old=grant()))
    assert "revoked" in text and ":no_entry:" in text


def test_edits_to_an_already_revoked_row_are_invisible():
    assert notify(ev("access_grant", "UPDATE",
                     new=grant(revoked_at="2026-01-02", tier="ro"),
                     old=grant(revoked_at="2026-01-01", tier="rw"))) == []


def test_a_tier_change_is_reported_with_the_new_tier():
    (_, text), = notify(ev("access_grant", "UPDATE",
                           new=grant(tier="ddl"), old=grant(tier="ro")))
    assert "*DDL*" in text


def test_a_change_with_nothing_visible_in_it_is_silent():
    assert notify(ev("access_grant", "UPDATE", new=grant(), old=grant())) == []


def test_the_merge_flag_is_explained_in_words_not_column_names():
    """`merge_with_team` decides whether a row adds to a team's access or
    replaces it, which is the difference between gaining and losing access.
    Nobody should have to know the column to understand the message."""
    (_, text), = notify(ev("access_grant", "UPDATE",
                           new=grant(merge_with_team=True),
                           old=grant(merge_with_team=False)))
    assert "adds to" in text and "merge_with_team" not in text


# --- a waiver reads differently from a grant --------------------------------


def test_a_waiver_talks_about_waiting_not_about_access():
    """It grants nothing. Telling somebody they were "granted RO" when they
    already had it, and all that changed is the wait, is a wrong message."""
    (_, text), = notify(ev("access_grant", "INSERT",
                           grant(auto_approve=True, tier="ro")))
    assert "skipped" in text and "Access granted" not in text


def test_revoking_a_waiver_says_automatic_approval_not_access():
    (_, text), = notify(ev("access_grant", "UPDATE",
                           new=grant(auto_approve=True, revoked_at="x"),
                           old=grant(auto_approve=True)))
    assert "automatic approval" in text


# --- a team grant reaches the team ------------------------------------------


def test_a_team_grant_goes_to_every_member():
    got = notify(ev("access_grant", "INSERT",
                    grant(principal_id=None, team_id=7), user=None, team_id=7),
                 members=("U1", "U2", "U3"))
    assert {who for who, _ in got} == {"U1", "U2", "U3"}
    assert all("team `platform-team`" in text for _, text in got)


def test_a_team_grant_with_no_resolvable_members_says_nothing():
    """Better silent than a crash in a poller that then stops delivering
    everyone else's messages."""
    assert notify(ev("access_grant", "INSERT",
                     grant(principal_id=None, team_id=7), user=None, team_id=7),
                  members=()) == []


# --- roles ------------------------------------------------------------------


def test_a_team_lead_approver_is_told_the_scope_and_the_ceiling():
    row = {"role": "approver", "scope_team_id": 7, "all_teams": False,
           "scope_target_id": None, "all_targets": True, "max_tier": "rw",
           "any_tier": False, "valid_until": None, "revoked_at": None}
    (_, text), = notify(ev("role_assignment", "INSERT", row))
    assert "approver" in text and "platform-team" in text and "*RW*" in text


def test_a_permanent_role_does_not_trail_off_about_expiry():
    row = {"role": "admin", "scope_team_id": None, "all_teams": True,
           "scope_target_id": None, "all_targets": True, "max_tier": None,
           "any_tier": True, "valid_until": None, "revoked_at": None}
    (_, text), = notify(ev("role_assignment", "INSERT", row))
    assert text.rstrip().endswith("admin*.") and "permanent" not in text


def test_losing_a_role_says_so():
    row = {"role": "approver", "scope_team_id": None, "all_teams": True,
           "scope_target_id": None, "all_targets": True, "max_tier": None,
           "any_tier": True, "revoked_at": "2026-09-07"}
    (_, text), = notify(ev("role_assignment", "UPDATE", new=row,
                           old={**row, "revoked_at": None}))
    assert "no longer" in text


# --- the whitelist and the dials --------------------------------------------


def test_switching_a_principal_off_explains_what_survives():
    """Their grants are untouched; nothing will run. Saying only "disabled"
    invites a support question about lost access."""
    (_, text), = notify(ev("principal", "UPDATE", {"enabled": False},
                           {"enabled": True}))
    assert "switched off" in text and "grants are unchanged" in text


def test_switching_a_principal_on_says_so():
    (_, text), = notify(ev("principal", "UPDATE", {"enabled": True},
                           {"enabled": False}))
    assert "enabled" in text


def test_a_cosmetic_principal_edit_is_silent():
    assert notify(ev("principal", "UPDATE", {"enabled": True, "tz": "UTC"},
                     {"enabled": True, "tz": None})) == []


def test_a_row_limit_override_is_announced():
    (_, text), = notify(ev("principal_setting", "INSERT",
                           {"setting_key": "max_rows", "setting_value": "500000",
                            "valid_until": None}))
    assert "500000" in text


def test_a_metrics_exclusion_is_not_worth_a_message():
    assert notify(ev("principal_setting", "INSERT",
                     {"setting_key": "exclude_from_metrics",
                      "setting_value": "true"})) == []


# --- team membership --------------------------------------------------------


def test_joining_and_leaving_a_team_are_both_announced():
    joined, = notify(ev("team_member", "INSERT", {"team_id": 7}))
    left, = notify(ev("team_member", "DELETE", old={"team_id": 7}))
    assert "added to team" in joined[1] and "removed from" in left[1]


def test_becoming_the_lead_is_announced():
    (_, text), = notify(ev("team_member", "UPDATE",
                           new={"team_id": 7, "is_lead": True},
                           old={"team_id": 7, "is_lead": False}))
    assert "lead" in text


# --- the two id spaces ------------------------------------------------------


def test_the_team_lookup_is_told_which_model_the_event_came_from():
    """`team_target_grants.team_id` points at `teams` and
    `access_grant.team_id` points at `team`. Team 3 is a different team in
    each, so the number alone cannot say where to look — reading the wrong one
    names the wrong team and DMs the wrong people."""
    seen = []

    def spy(tid, table=None):
        seen.append((tid, table))
        return "t", ["U1"]

    ae.build_notifications(
        ev("access_grant", "INSERT", grant(principal_id=None, team_id=7),
           user=None, team_id=7),
        alias_of=lambda t: None, team_info=spy)
    assert seen and seen[0][1] == "access_grant"


@pytest.mark.parametrize("table,expected_new", [
    ("access_grant", True), ("role_assignment", True), ("team_member", True),
    ("principal", True), ("principal_setting", True),
    ("team_target_grants", False), ("team_members", False)])
def test_the_lookup_knows_which_tables_belong_to_the_new_model(table, expected_new):
    assert (table in ae._NEW_MODEL_TABLES) is expected_new


def test_an_unknown_table_is_marked_processed_rather_than_retried_forever():
    assert ae.build_notifications(ev("something_new", "INSERT", {})) == []
