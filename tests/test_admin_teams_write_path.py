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


def _legacy_reach_outside_the_else(tree, patterns):
    """Every legacy-table SQL string, and every call to a `*_legacy` helper,
    that is NOT on the old-model side of a `use_v2()` branch.

    Scoped by BRANCH, not by function. The first version of this guard passed a
    function if `use_v2()` appeared anywhere in it, and the effective-access
    route shows why that was not enough: its memberships asked the switch, and
    four other reads a few lines below went straight to the retired tables.
    A `*_legacy` function is the old model's body by name; it may hold legacy
    SQL freely, and is itself checked at every place it is called.
    """
    import ast
    offenders = []

    aliases: set = set()        # names a function bound to the switch: v2 = use_v2()

    def asks_switch(test):
        # `_v2()` is admins.py's name for the same switch.
        if "use_v2()" in ast.unparse(test) or "_v2()" in ast.unparse(test):
            return True
        inner = test.operand if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) else test
        return isinstance(inner, ast.Name) and inner.id in aliases

    def is_negated(test):
        return isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)

    def ends(block):
        return bool(block) and isinstance(block[-1], (ast.Return, ast.Raise, ast.Continue))

    def walk_block(stmts, legacy_side, fn, parent):
        # `if use_v2(): ...; return` puts every statement after it in the same
        # block on the old-model side -- the early-return shape the codebase
        # uses far more often than an explicit else.
        after = False
        for st in stmts:
            walk(st, legacy_side or after, fn, parent)
            if isinstance(st, ast.If) and asks_switch(st.test) \
                    and not is_negated(st.test) and ends(st.body):
                after = True

    def walk(node, legacy_side, fn, parent):
        if isinstance(node, (ast.If, ast.IfExp)) and asks_switch(node.test):
            neg = is_negated(node.test)
            walk(node.test, legacy_side, fn, node)
            if isinstance(node, ast.If):
                walk_block(node.body, legacy_side or neg, fn, node)
                walk_block(node.orelse, legacy_side or not neg, fn, node)
            else:
                walk(node.body, legacy_side or neg, fn, node)
                walk(node.orelse, legacy_side or not neg, fn, node)
            return
        if isinstance(node, ast.FunctionDef):
            aliases.clear()
            aliases.update(t.id for a in ast.walk(node) if isinstance(a, ast.Assign)
                           and "use_v2()" in ast.unparse(a.value)
                           for t in a.targets if isinstance(t, ast.Name))
            walk_block(node.body, node.name.endswith("_legacy"), node.name, node)
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and not isinstance(parent, ast.Expr):          # a docstring is not SQL
            if not legacy_side and any(t in node.value for t in patterns):
                offenders.append(f"{fn}: {node.value.strip()[:60]!r}")
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", "")
            if name.endswith("_legacy") and not legacy_side:
                offenders.append(f"{fn}: calls {name}() outside the else branch")
        for field, value in ast.iter_fields(node):
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                walk_block(value, legacy_side, fn, node)
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, ast.AST):
                        walk(child, legacy_side, fn, node)
            elif isinstance(value, ast.AST):
                walk(value, legacy_side, fn, node)

    walk_block(tree.body, False, "<module>", tree)
    return offenders


LEGACY = ("FROM teams ", "INTO teams ", "UPDATE teams ", "JOIN teams ", "DELETE FROM teams ",
          "FROM team_members", "INTO team_members", "JOIN team_members",
          "FROM team_target_grants", "INTO team_target_grants",
          "UPDATE team_target_grants", "DELETE FROM team_target_grants",
          "JOIN team_target_grants")


def test_no_route_reaches_the_legacy_team_tables_unconditionally():
    """The guard that stops the next copy.

    `team_target_grants` and a bare JOIN were missing from the first pattern
    list, which is how the grant list and the grant revoke kept the legacy
    table after every other route had moved.
    """
    import ast
    tree = ast.parse(inspect.getsource(routes_admin))
    offenders = _legacy_reach_outside_the_else(tree, LEGACY)
    assert not offenders, (
        "these reach the legacy team tables without asking the switch:\n  "
        + "\n  ".join(offenders))


def test_the_guard_catches_a_read_beside_a_guarded_one():
    """The shape that got through the first guard: one read asks the switch,
    the next does not."""
    import ast
    src = (
        "def route():\n"
        "    a = q('SELECT 1 FROM team') if teams_mod.use_v2() else q('SELECT 1 FROM teams x')\n"
        "    b = q('SELECT 1 FROM team_target_grants g')\n")
    assert _legacy_reach_outside_the_else(ast.parse(src), LEGACY) == \
        ["route: 'SELECT 1 FROM team_target_grants g'"]


def test_the_guard_accepts_the_else_branch_and_a_legacy_helper():
    import ast
    src = (
        "def _x_legacy():\n"
        "    return q('SELECT 1 FROM team_target_grants g')\n"
        "def route():\n"
        "    if teams_mod.use_v2():\n"
        "        return q('SELECT 1 FROM access_grant')\n"
        "    else:\n"
        "        return _x_legacy()\n")
    assert _legacy_reach_outside_the_else(ast.parse(src), LEGACY) == []


def test_the_guard_refuses_a_legacy_helper_called_unconditionally():
    import ast
    src = ("def _x_legacy():\n    return 1\n"
           "def route():\n    return _x_legacy()\n")
    assert _legacy_reach_outside_the_else(ast.parse(src), LEGACY) == \
        ["route: calls _x_legacy() outside the else branch"]


def test_the_guard_understands_an_early_return():
    import ast
    src = ("def route():\n"
           "    if teams_mod.use_v2():\n"
           "        return q('SELECT 1 FROM team')\n"
           "    return q('SELECT 1 FROM teams x')\n")
    assert _legacy_reach_outside_the_else(ast.parse(src), LEGACY) == []


def test_the_guard_understands_the_switch_held_in_a_variable():
    import ast
    src = ("def route():\n"
           "    v2 = teams_mod.use_v2()\n"
           "    if v2:\n"
           "        q('SELECT 1 FROM team')\n"
           "    else:\n"
           "        q('SELECT 1 FROM team_members m')\n"
           "    q('SELECT 1 FROM access_grant' if v2 else 'SELECT 1 FROM teams x')\n")
    assert _legacy_reach_outside_the_else(ast.parse(src), LEGACY) == []



# --- the whole package ---------------------------------------------------------
#
# The same bug class lived outside the admin routes too: the connection delete
# gate counted only the legacy grant tables, and a metrics panel reported 0 team
# grants for 35. Two shapes are correct without a branch and are allowed:
#
# * one statement that reads BOTH models -- whichever side is empty adds
#   nothing, so the answer is right on either side of the switch;
# * a function that picks the model by where its DATA came from, not by the
#   switch. Listed by name, with the reason, so adding one is a decision.

_NEW_MODEL = ("access_grant", "FROM team ", "JOIN team ", "team_member ")

_CHOSEN_BY_ORIGIN = {
    # An outbox row names the table it was captured on; a row written before
    # the switch must still resolve against the model it came from, and the two
    # number teams independently.
    ("auth_events.py", "_team_info"),
    # A warning carries its grant's kind; a legacy team grant's recipients are
    # in the legacy membership table, a new one's in team_member.
    ("grant_expiry.py", "recipients_for"),
}


def test_no_module_reaches_the_legacy_team_tables_unconditionally():
    import ast
    import pathlib
    root = pathlib.Path(routes_admin.__file__).resolve().parents[1]
    offenders = []
    for f in sorted(root.rglob("*.py")):
        src = f.read_text(encoding="utf-8")
        if not any(t in src for t in LEGACY):
            continue
        tree = ast.parse(src)
        both = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and any(t in n.value for t in LEGACY)
                and any(t in n.value for t in _NEW_MODEL)}
        rel = str(f.relative_to(root))
        for o in _legacy_reach_outside_the_else(tree, LEGACY):
            fn, _, text = o.partition(": ")
            if (rel, fn) in _CHOSEN_BY_ORIGIN:
                continue
            if any(b.strip().startswith(text.strip("'\"")[:40]) for b in both):
                continue
            offenders.append(f"{rel} {o}")
    assert not offenders, (
        "these reach the legacy team tables without asking the switch:\n  "
        + "\n  ".join(offenders))


def test_the_origin_exceptions_still_exist():
    """An allowance for a function that is gone is an allowance for its
    replacement, which nobody reviewed."""
    import pathlib
    root = pathlib.Path(routes_admin.__file__).resolve().parents[1]
    for rel, fn in _CHOSEN_BY_ORIGIN:
        assert f"def {fn}(" in (root / rel).read_text(encoding="utf-8"), (rel, fn)
