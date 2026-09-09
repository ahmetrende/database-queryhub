"""The copy-paste recipe on the access-request card names tables that exist.

The card hands an admin SQL to run before pressing Approve. It targeted the
legacy `teams` / `team_members` / `team_target_grants`, and after the pod
cutover those are empty -- so `(SELECT id FROM teams WHERE name = 'TEAM_NAME')`
returned NULL and the admin's paste failed on a not-null column.

A broken recipe on the card is worse than no recipe: it is read as the
supported way to do this, so the person who runs it concludes the database is
wrong rather than the card.

Both statements were run against the real schema in a rolled-back transaction
before this shipped, which is the only way to know generated SQL parses and
matches the constraints it claims to.
"""
import inspect

from queryhub.slack_app import access


def _src() -> str:
    return inspect.getsource(access.admin_dm_blocks)


def test_the_snippet_follows_the_switch():
    src = _src()
    assert "teams_mod.use_v2()" in src
    assert "INSERT INTO team_member (team_id, principal_id)" in src   # new
    assert "INSERT INTO team_members (team_id, slack_user_id)" in src  # legacy


def test_the_new_recipe_writes_where_the_resolver_reads():
    """A team grant lives in `access_grant` under the new model;
    `team_target_grants` is not read by anything once the switch is on."""
    src = _src()
    i = src.index("teams_mod.use_v2()")
    v2 = src[i:src.index("    else:", i)]
    assert "INSERT INTO access_grant" in v2
    assert "team_target_grants" not in v2


def test_the_recipe_accepts_the_name_people_say_out_loud():
    """A pod's `name` is the importer's code and its `display_name` is what
    everybody calls it. An admin typing what the screen shows must not get
    zero rows and no error."""
    src = _src()
    assert "t.name = 'TEAM_NAME' OR t.display_name = 'TEAM_NAME'" in src


def test_the_membership_insert_matches_the_partial_index():
    """`team_member_uq` is `(team_id, principal_id) WHERE NOT is_deleted`.
    Without the predicate the inference does not match and PostgreSQL raises
    -- in the admin's psql session, which is the worst place to find out."""
    assert "ON CONFLICT (team_id, principal_id) WHERE NOT is_deleted " in _src()


def test_soft_deleted_teams_are_not_offered():
    src = _src()
    i = src.index("teams_mod.use_v2()")
    v2 = src[i:src.index("    else:", i)]
    assert v2.count("NOT t.is_deleted") >= 2
    assert "NOT i.is_deleted" in v2


def test_the_grant_says_where_it_came_from():
    """`reason` is what an auditor reads months later."""
    assert "granted from an access request" in _src()
