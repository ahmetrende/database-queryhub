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


# --- rule 4 is decided per server ---------------------------------------------


def _db(name, **kw):
    return row(all_databases=False, database_name=name, **kw)


def test_an_own_grant_on_one_database_displaces_the_team_on_the_others():
    """A pod split put two pods' databases on one server: the team kept one,
    and a member got a personal grant on the other. Decided per database, the
    team's grant still applied to them for submit and on the effective-access
    screen, while the picker (which lists the server) hid it. Per server, as
    the old model did it, the own grant decides the whole server."""
    server = [_db("ledger", tier="ro"), _db("orders", mine=False, tier="rw")]
    assert access._decide(access._on_database(server, "orders"), server) is None
    assert access._decide(access._on_database(server, "ledger"), server)["tier"] == "ro"


def test_merging_the_own_grant_keeps_the_team_on_the_other_database():
    server = [_db("ledger", tier="ro", merge=True), _db("orders", mine=False, tier="rw")]
    got = access._decide(access._on_database(server, "orders"), server)
    assert (got["tier"], got["source"]) == ("rw", "team")


def test_an_ended_own_grant_leaves_nothing_on_the_whole_server():
    """Rule 2 per server: nothing of theirs is live and one has ended, so the
    team's grant on another database does not apply either."""
    server = [_db("ledger", expired=True), _db("orders", mine=False, tier="rw")]
    assert access._decide(access._on_database(server, "orders"), server) is None


def test_a_waiver_on_another_database_displaces_nothing():
    server = [_db("ledger", auto=True), _db("orders", mine=False, tier="rw")]
    assert access._decide(access._on_database(server, "orders"), server)["tier"] == "rw"


def _shapes():
    """Every small server a principal can hold: up to two own rows and up to
    two team rows, over two databases, each own row live, ended, not started
    or a waiver, merging or not."""
    import itertools
    own = [None]
    for where in ("a", "b", "*"):
        for state in ("live", "ended", "later", "waiver"):
            for merge in (False, True):
                kw = {"mine": True, "merge": merge, "tier": "ro",
                      "expired": state == "ended", "not_started": state == "later",
                      "auto": state == "waiver"}
                own.append(row(**kw) if where == "*" else _db(where, **kw))
    team = [[], [_db("a", mine=False, tier="rw")], [_db("b", mine=False, tier="ddl")],
            [row(mine=False, tier="rw")],
            [_db("a", mine=False, tier="rw"), _db("b", mine=False, tier="ro")]]
    for o1, o2 in itertools.combinations_with_replacement(range(len(own)), 2):
        mine = [own[i] for i in {o1, o2} if own[i] is not None]
        for t in team:
            yield mine + t


def test_the_list_and_the_tier_authority_never_disagree(monkeypatch):
    """The invariant this bug broke: a database is reachable by `resolve`
    exactly when `resolve_target` lists it, and `resolve_databases` (the
    effective-access screen) answers as `resolve` does. Checked through the
    public functions, over every shape `_shapes` builds."""
    state = {}
    monkeypatch.setattr(access, "is_admin", lambda pid: False)
    monkeypatch.setattr(access, "_covering", lambda pid, tid, dbn: [
        r for r in state["rows"]
        if dbn is None or r["all_databases"] or r["database_name"] == dbn])
    monkeypatch.setattr(access.db, "fetch_all", lambda sql, params=None: [
        {**r, "target_id": 53, "team_id": None, "valid_until": None}
        for r in state["rows"]])
    checked = 0
    for rows in _shapes():
        state["rows"] = rows
        listed = access.resolve_target("U1", 53)
        batch = access.resolve_databases("U1", [(53, "a"), (53, "b")])
        for d in ("a", "b"):
            one = access.resolve("U1", 53, d)
            in_list = listed is not None and (listed["databases"] is None
                                              or d in listed["databases"])
            assert (one is not None) == in_list, (d, rows)
            assert (batch[(53, d)][0] is not None) == (one is not None), (d, rows)
            if one is not None:
                assert batch[(53, d)][0]["tier"] == one["tier"], (d, rows)
            checked += 1
    assert checked > 1000


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
