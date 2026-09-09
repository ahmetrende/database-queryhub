"""The Teams screen writes to the model it reads from.

The read path started answering from the nine-table model when `_teams_payload`
followed the switch. The three mutation routes did not: they addressed the
legacy `teams` table by the id the list had handed out. Those are different
tables with different sequences, so after the pod cutover the screen listed ids
14-26 while `teams` was empty with its sequence at 10. A rename reported success
and changed nothing; a delete answered 404 for every team on the screen.

The part that made it worth fixing rather than noting: the two id spaces were
about to overlap. Four teams created through the web would have taken the
legacy sequence past 14, and from then on a rename would have edited a
DIFFERENT team than the one on screen, silently.

A synced team is refused rather than edited, because the importer that owns it
reconciles on its next run -- an edit that gets quietly undone is worse than
one that is refused.
"""
import inspect

from queryhub.web import routes_admin


def _code(fn) -> str:
    return "\n".join(ln for ln in inspect.getsource(fn).splitlines()
                     if not ln.lstrip().startswith("#"))


def test_the_lookup_follows_the_switch():
    """One resolver for all three routes: three copies is how one of them
    keeps the old table."""
    src = _code(routes_admin._team_for_write)
    assert "teams_mod.use_v2()" in src
    assert "FROM team " in src        # new model
    assert "FROM teams " in src       # and the legacy fallback


def test_every_mutation_resolves_through_it():
    for fn in (routes_admin.admin_update_team, routes_admin.admin_delete_team):
        assert "_team_for_write(team_id)" in _code(fn), fn.__name__
        # the legacy BRANCH keeps its own statements; what must be gone
        # is the unconditional lookup that ignored the switch
        assert "SELECT id, name FROM teams WHERE id" not in _code(fn), fn.__name__


def test_all_three_writes_follow_the_switch():
    for fn in (routes_admin.admin_create_team, routes_admin.admin_update_team,
               routes_admin.admin_delete_team):
        assert "teams_mod.use_v2()" in _code(fn), fn.__name__


def test_a_synced_team_is_refused_not_silently_reconciled():
    for fn in (routes_admin.admin_update_team, routes_admin.admin_delete_team):
        assert "_refuse_synced_team(team)" in _code(fn), fn.__name__
    src = _code(routes_admin._refuse_synced_team)
    assert 'team.get("source")' in src
    assert "409" in src


def test_a_team_made_here_is_not_claimed_by_an_importer():
    """`source` stays NULL on a hand-made team; the importers only reconcile
    rows carrying their own source, so a NULL one survives every sync."""
    src = _code(routes_admin.admin_create_team)
    i = src.index("INSERT INTO team ")
    assert "source" not in src[i:i + 200]


def test_membership_insert_matches_the_partial_unique_index():
    """`team_member_uq` is `(team_id, principal_id) WHERE NOT is_deleted`.
    An ON CONFLICT inference without that predicate does not match the index
    and PostgreSQL raises instead of doing nothing."""
    src = _code(routes_admin.admin_update_team)
    assert "ON CONFLICT (team_id, principal_id) WHERE NOT is_deleted " in src


def test_removing_a_member_soft_deletes():
    """Every other reader of `team_member` filters `is_deleted`; a hard DELETE
    would also drop the record that the membership once existed."""
    src = _code(routes_admin.admin_update_team)
    i = src.index("teams_mod.use_v2()")
    v2 = src[i:src.index("else:", i)]
    assert "SET is_deleted = TRUE" in v2
    assert "DELETE FROM team_member" not in v2


def test_setting_a_persons_teams_validates_against_the_live_model():
    """The fourth copy of the same seam, on the People tab. Ids from a v2
    screen were checked against `teams`, so every one of them fell out and the
    save set the person's membership to nothing."""
    src = _code(routes_admin.admin_set_person_teams)
    assert "teams_mod.use_v2()" in src
    assert "FROM team WHERE id = ANY(%s) AND NOT is_deleted" in src
    assert "ON CONFLICT (team_id, principal_id) WHERE NOT is_deleted " in src


def test_deleting_a_team_revokes_the_grants_that_name_it():
    """`access_grant` rows keep answering otherwise: the resolver joins the
    team by id and does not check `team.is_deleted`."""
    src = _code(routes_admin.admin_delete_team)
    assert "UPDATE access_grant SET revoked_at" in src
    assert "team_id = %s" in src


# --- the same seam on two read/copy routes -----------------------------------
#
# Six copies in one file, found by sweeping for unconditional access to the
# legacy team tables. The pattern is always the same: the screen lists from
# whichever model is live, and the route behind it addresses `teams` /
# `team_members` regardless. Emptied by the pod cutover, they answer "nothing"
# rather than failing, which is why none of this surfaced as an error.


def test_effective_access_reads_memberships_from_the_live_model():
    """An admin asks this screen what somebody can reach BEFORE deciding
    something. Against the legacy tables it answered "no teams" for every
    person on the fleet."""
    src = _code(routes_admin.admin_effective_access)
    assert "teams_mod.use_v2()" in src
    assert "FROM team_member m JOIN team t" in src


def test_copy_access_copies_memberships_from_the_live_model():
    """"Give this person what that person has" silently copied no teams, and
    reported success in the past tense."""
    src = _code(routes_admin.admin_copy_access)
    assert "teams_mod.use_v2()" in src
    assert "INSERT INTO team_member (team_id, principal_id)" in src
    assert "ON CONFLICT (team_id, principal_id) WHERE NOT is_deleted " in src


def test_no_route_reaches_the_legacy_team_tables_unconditionally():
    """The guard that stops a seventh copy appearing.

    Checked per FUNCTION, not per line: the legacy statements survive inside
    the `else` branch of a route that asked the switch, and any fixed lookback
    window is either too short to clear a long v2 branch or long enough to
    swallow the next function.
    """
    import re
    src = inspect.getsource(routes_admin)
    LEGACY = ("FROM teams", "INTO teams", "UPDATE teams",
              "FROM team_members", "INTO team_members", "DELETE FROM teams")
    offenders = []
    # split on top-level defs, keeping each function with its own body
    parts = re.split(r"\n(?=def )", src)
    for part in parts:
        name = part.split("(", 1)[0].removeprefix("def ").strip()
        body = "\n".join(ln for ln in part.splitlines()
                          if not ln.lstrip().startswith("#"))
        if not any(t in body for t in LEGACY):
            continue
        if "use_v2()" not in body:
            offenders.append(name)
    assert not offenders, (
        "these reach the legacy team tables without asking the switch: "
        f"{offenders}")
