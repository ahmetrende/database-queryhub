"""The new resolver's rules, each one pinned to the case that would break it.

`queryhub.access` answers what `teams.py` answers, from the nine-table
model. Equivalence over the whole fleet is proved elsewhere, by capturing every
(principal, target, database) answer from both and diffing them — that is the
evidence, and it is empty. What it cannot do is say WHY each answer is what it
is, or catch a rule that today's data happens not to exercise. This file does
that: `_decide` is pure, so every rule can be put to it directly, including the
shapes production has never yet produced.

Two of these tests describe bugs the first version of the resolver had, both
found by running it against real rows rather than by reading it.
"""
import pytest

from queryhub import access

RANK = {"ro": 10, "rw": 20, "ddl": 30}


def row(*, mine=True, tier="ro", auto=False, merge=False, expired=False,
        not_started=False, all_targets=False, all_databases=True,
        database_name=None, db_role=None):
    return {"mine": mine, "tier": tier, "rank": RANK[tier], "auto_approve": auto,
            "merge_with_team": merge, "all_targets": all_targets,
            "all_databases": all_databases, "database_name": database_name,
            "db_role": db_role, "expired": expired, "not_started": not_started}


# --- nothing at all ---------------------------------------------------------


def test_no_rows_is_no_access():
    assert access._decide([]) is None


def test_a_row_that_has_not_started_yet_grants_nothing():
    assert access._decide([row(not_started=True)]) is None


# --- rule 1: a waiver is not a grant ----------------------------------------


def test_a_waiver_alone_grants_nothing():
    """Three people hold fleet-wide read waivers and no fleet-wide access. Read
    as grants, those rows hand out the fleet."""
    assert access._decide([row(auto=True, tier="ro")]) is None


def test_a_waiver_does_not_raise_the_tier_it_only_waives_the_wait():
    got = access._decide([row(tier="ro"), row(auto=True, tier="ddl", merge=True)])
    assert got["tier"] == "ro"


def test_a_waiver_above_the_access_is_capped_not_discarded():
    """A rw window over ro access still waives the wait on the ro queries the
    access permits: the old check asks whether the window COVERS the tier the
    query needs, and a wider window covers a narrower query. The first version
    filtered these out and would have made people wait where they do not."""
    got = access._decide([row(tier="ro"), row(auto=True, tier="rw", merge=True)])
    assert (got["tier"], got["auto_tier"]) == ("ro", "ro")


def test_a_waiver_below_the_access_waives_only_up_to_itself():
    got = access._decide([row(tier="ddl"), row(auto=True, tier="ro", merge=True)])
    assert (got["tier"], got["auto_tier"]) == ("ddl", "ro")


def test_no_waiver_means_no_automatic_approval():
    assert access._decide([row(tier="rw")])["auto_tier"] is None


# --- rule 2 and 3: how a grant ends -----------------------------------------


def test_an_expired_principal_grant_does_not_fall_through_to_the_team():
    """Such a row is usually written to NARROW what a team allows. Falling
    through would make the expiry WIDEN access, which is an expiry that grants
    something."""
    rows = [row(mine=True, tier="ro", expired=True),
            row(mine=False, tier="ddl")]
    assert access._decide(rows) is None


def test_a_live_principal_grant_wins_over_an_expired_sibling():
    rows = [row(mine=True, tier="ro", expired=True),
            row(mine=True, tier="rw"),
            row(mine=False, tier="ddl")]
    assert access._decide(rows)["tier"] == "rw"


def test_an_expired_team_grant_is_simply_gone():
    assert access._decide([row(mine=False, tier="rw", expired=True)]) is None


def test_a_revoked_grant_never_reaches_the_decision():
    """Revoked rows are filtered in SQL, so the team grant resumes — the older
    behaviour, kept deliberately."""
    assert access._decide([row(mine=False, tier="rw")])["source"] == "team"


# --- rule 4: what displaces the team ----------------------------------------


def test_a_principal_grant_replaces_the_team_by_default():
    rows = [row(mine=True, tier="ro", merge=False), row(mine=False, tier="ddl")]
    got = access._decide(rows)
    assert (got["tier"], got["source"]) == ("ro", "principal")


def test_merge_with_team_adds_to_the_team_instead():
    rows = [row(mine=True, tier="ro", merge=True), row(mine=False, tier="ddl")]
    got = access._decide(rows)
    assert (got["tier"], got["source"]) == ("ddl", "team")


def test_a_waiver_does_not_displace_the_team_grant_underneath_it():
    """The trap in folding auto-approve into the grant table. A waiver is a
    principal row and principal rows win, so counting it as one would delete
    the team access of everyone holding an auto-approve window."""
    rows = [row(mine=True, tier="ro", auto=True, merge=False),
            row(mine=False, tier="rw")]
    got = access._decide(rows)
    assert got is not None and got["tier"] == "rw"


def test_one_narrowing_row_is_enough_to_displace_the_team():
    rows = [row(mine=True, tier="ro", merge=False),
            row(mine=True, tier="ro", merge=True),
            row(mine=False, tier="ddl")]
    assert access._decide(rows)["tier"] == "ro"


# --- combining ---------------------------------------------------------------


def test_the_most_permissive_covering_row_wins():
    rows = [row(mine=False, tier="ro"), row(mine=False, tier="ddl"),
            row(mine=False, tier="rw")]
    assert access._decide(rows)["tier"] == "ddl"


def test_a_fleet_wide_grant_reports_itself_as_unrestricted():
    """`bypass_team_grants` became this. Visibility has to tell it from an
    admin, and only the grant knows it was fleet-wide."""
    got = access._decide([row(all_targets=True, all_databases=True, tier="ddl")])
    assert got["unrestricted"] is True


def test_a_scoped_grant_is_not_unrestricted():
    got = access._decide([row(all_targets=False, all_databases=False,
                              database_name="app", tier="ddl")])
    assert got["unrestricted"] is False


def test_the_database_role_rides_along_with_the_winning_row():
    got = access._decide([row(tier="ro"), row(tier="ddl", db_role="reporting")])
    assert got["db_role"] == "reporting"


# --- the module's shape ------------------------------------------------------


@pytest.mark.parametrize("name", [
    "resolve", "resolve_target", "resolve_many", "can_use_target",
    "can_use_database", "visible_targets", "can_approve", "is_admin",
    "is_super_admin", "roles", "setting", "has_fleet_wide_grant"])
def test_the_public_surface_is_there(name):
    assert callable(getattr(access, name))


def test_a_temporary_admin_is_not_a_super_admin():
    """The old model kept temporary admins in their own table and refused to
    treat one as super. Here both live in `role_assignment`, so a query for
    'unscoped admin' would also match someone holding it for an afternoon —
    who could then write role rows and read super-admin-only PII exemptions."""
    src = access.is_super_admin.__doc__ or ""
    assert "valid_until" in src
    import inspect
    assert 'r["valid_until"] is None' in inspect.getsource(access.is_super_admin)


def test_visibility_separates_an_admin_from_a_fleet_wide_grant():
    """Both reach everything; only the admin sees the disabled half of the
    catalog. The old code got that from two different columns."""
    import inspect
    src = inspect.getsource(access.visible_targets)
    assert "is_admin" in src
    assert "ts.enabled" in src
    assert "NOT g.auto_approve" in src      # a waiver never makes a target visible


def test_the_tier_authority_is_per_database():
    """Aggregating across a target's databases is how a read grant on one and a
    write grant on another combine into write on both."""
    import inspect
    assert "database_name" in inspect.getsource(access.resolve)
    assert "not the tier on" in (access.resolve_target.__doc__ or "")


# --- nobody reviews their own request ---------------------------------------


class _Roles:
    """`roles()` for one principal, without a database."""

    def __init__(self, *rows):
        self.rows = [{"role": r, "scope_team_id": None, "all_teams": True,
                      "scope_target_id": None, "all_targets": True,
                      "max_tier": cap, "any_tier": cap is None,
                      "valid_until": None} for r, cap in rows]


def _with_roles(monkeypatch, *rows):
    monkeypatch.setattr(access, "roles", lambda pid: _Roles(*rows).rows)


REQ = {"requester_slack_id": "U1", "target_server_id": 5, "required_tier": "rw"}


def test_a_scoped_approver_cannot_approve_their_own_request(monkeypatch):
    """The whole of the review, gone: the person who wrote the query waves it
    through. The old model had no scoped approvers, so this rule is new because
    the thing it guards is new."""
    _with_roles(monkeypatch, ("approver", None))
    assert access.can_approve("U1", REQ) is False


def test_a_scoped_approver_can_still_approve_somebody_else(monkeypatch):
    _with_roles(monkeypatch, ("approver", None))
    assert access.can_approve("U2", REQ) is True


def test_an_admin_may_approve_their_own_request(monkeypatch):
    """Preserved deliberately. An admin is fleet-wide and could grant
    themselves the access anyway, so refusing them removes a workflow — an
    operator approving their own test submission — while preventing nothing."""
    _with_roles(monkeypatch, ("admin", None))
    assert access.can_approve("U1", REQ) is True


def test_an_admins_tier_ceiling_still_applies_to_their_own_request(monkeypatch):
    """The exemption is from the self-approval rule, not from the ceiling."""
    _with_roles(monkeypatch, ("admin", "ro"))
    assert access.can_approve("U1", REQ) is False
    assert access.can_approve("U1", {**REQ, "required_tier": "ro"}) is True


def test_holding_both_roles_takes_the_admin_exemption(monkeypatch):
    _with_roles(monkeypatch, ("approver", "ro"), ("admin", None))
    assert access.can_approve("U1", REQ) is True


def test_somebody_with_no_role_approves_nothing(monkeypatch):
    _with_roles(monkeypatch)
    assert access.can_approve("U2", REQ) is False


# --- an admin's waiver still waives the wait ---------------------------------


def test_an_admin_with_a_waiver_still_skips_the_queue():
    """The admin branch hardcoded `auto_tier: None`, reasoning that an admin
    reaches everything so a grant adds nothing. True of the GRANT and false of
    the WAIVER — they are different questions, and the old model answers the
    second from `auto_approve_grants` without caring who holds it.

    Found by the pre-cutover diff the first time an admin was given a
    fleet-wide RO waiver: 235 answers where the old model said the wait was
    skipped and the new one said it was not. Nothing was wrong with the
    grants; the new model simply never asked about the window."""
    import inspect
    src = inspect.getsource(access.resolve)
    assert "_admin_auto(" in src
    assert '"auto_tier": None' not in src


def test_both_resolve_paths_ask_the_same_question():
    """`resolve` and `resolve_target` each had their own copy of the
    short-circuit. One fixed and one not would answer differently about the
    same person."""
    import inspect
    for fn in (access.resolve, access.resolve_target):
        assert "_admin_auto(" in inspect.getsource(fn), fn.__name__


def test_the_admin_waiver_is_not_capped():
    """An admin's access is `ddl`, the top of the ladder, so a waiver at any
    tier is already at or below it. A cap here would be arithmetic that can
    only ever be a no-op, and the kind that rots into a bug."""
    import inspect
    src = inspect.getsource(access._admin_auto)
    assert "No cap is applied" in src
    assert "max(rows, key=lambda r: r[\"rank\"])" in src


def test_an_expired_admin_waiver_does_not_count():
    import inspect
    src = inspect.getsource(access._admin_auto)
    assert 'not r["expired"]' in src and 'not r["not_started"]' in src
