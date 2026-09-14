"""The roles endpoints: who approves, who grants, who imports.

The nine-table model separates two things the `admins` table ran together —
being an administrator, and being allowed to approve a particular request. A
`role_assignment` row can say "approves for this team, up to RW, and nothing
else": the team lead who should see their own team's requests and no others,
which the old scope arrays could describe but no screen could set.

These are the only endpoints that write the new tables directly. Grants keep
going to the legacy tables and reach the new model through the migration 109
mirror, so what is pinned hardest here is the boundary between the two: the
mirror owns rows it marked, these own the rest, and neither may edit the
other's.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "src" / "queryhub" / "web" / "routes_admin.py").read_text(encoding="utf-8")


def _route(method: str, path: str) -> str:
    m = re.search(rf'@router\.{method}\("{re.escape(path)}".*?(?=\n@router\.|\Z)',
                  SRC, re.S)
    assert m, f"{method.upper()} {path} is gone"
    return m.group(0)


LIST = _route("get", "/roles")
CREATE = _route("post", "/roles")
REVOKE = _route("delete", "/roles/{role_id}")


# --- who may touch them ------------------------------------------------------


def test_reading_roles_needs_only_an_admin():
    assert 'require_admin(claims, "review")' in LIST


@pytest.mark.parametrize("body", [CREATE, REVOKE])
def test_changing_roles_needs_a_super_admin(body):
    """Granting somebody the power to approve is how an approval boundary is
    moved. It belongs with the people who can already move it."""
    assert 'require_admin(claims, "access")' in body


@pytest.mark.parametrize("body,action", [(CREATE, "role_granted"),
                                         (REVOKE, "role_revoked")])
def test_every_change_writes_an_audit_row_in_the_same_transaction(body, action):
    assert f'"{action}"' in body
    assert "audit.log_in(cur" in body


# --- the boundary with the mirror -------------------------------------------


def test_a_mirrored_role_cannot_be_revoked_here():
    """It projects the `admins` table and would come straight back on the next
    write to it. Refusing with the reason is better than a change that quietly
    undoes itself."""
    assert 'row["mirrored_from"]' in REVOKE
    assert "409" in REVOKE and "admins table" in REVOKE


def test_mirrored_roles_are_listed_and_labelled_rather_than_hidden():
    """They are what the resolver actually sees. A screen that omitted them
    would disagree with the answers people get."""
    assert '"mirrored" if r["mirrored_from"]' in SRC


def test_a_setting_is_still_never_written_directly_to_the_new_model():
    """Roles were the first thing written straight to the new tables, and team
    grants became the second when the pod cutover left pod teams with no
    legacy row to hang a grant on.

    `principal_setting` is NOT in that company and the distinction is the
    point: row limits and metrics exclusions still have a legacy home, the
    mirror still projects them, and a copy written here would sit beside the
    projection describing the same dial twice."""
    assert "INSERT INTO principal_setting" not in SRC
    assert "INSERT INTO role_assignment" in SRC


# --- the shapes the schema refuses, refused with a sentence ------------------


def test_an_admin_role_may_not_be_scoped():
    """The constraint refuses it too; saying so here gives a usable message
    instead of a constraint violation."""
    assert "fleet-wide" in CREATE
    assert 'body.role == "admin"' in CREATE


def test_an_unknown_role_is_refused_by_name():
    assert "_ROLES" in CREATE and "Unknown role" in CREATE


def test_the_role_vocabulary_is_only_roles_something_enforces():
    """`importer` was removed on 2026-09-09 and `granter` was wired.

    The pair had been written on the Roles screen while nothing read either, so
    the screen was showing an authority that decided nothing — which reads as
    protection that is not there. `granter` is now enforced by
    `grants._authz_v2`; `importer` had no holders and no reader, and the org
    importer runs as the operator, so it went. A role in this list must be a
    role some code checks.
    """
    m = re.search(r'_ROLES = \((.*?)\)', SRC, re.S)
    assert m
    assert set(re.findall(r'"(\w+)"', m.group(1))) == {
        "approver", "granter", "admin"}


def test_granter_is_actually_read_by_the_grant_path():
    """The point of keeping it. If this stops being true, the role is back to
    naming an authority nothing checks and belongs out of the vocabulary."""
    from pathlib import Path
    grants_src = (Path(__file__).resolve().parents[1] / "src" / "queryhub"
                  / "grants.py").read_text(encoding="utf-8")
    body = grants_src.split("def _authz_v2", 1)[1].split("\ndef ", 1)[0]
    assert "'granter'" in body or '"granter"' in body


def test_an_unknown_tier_is_refused():
    assert "maxTier must be" in CREATE


def test_a_person_with_no_principal_row_is_refused_not_created():
    """Creating one here would put somebody in the access model as a side
    effect of being named in a form."""
    assert "404" in CREATE and "need a QueryHub account first" in CREATE


def test_an_unknown_team_is_refused():
    assert "No such team." in CREATE


# --- the wildcards stay honest ----------------------------------------------


def test_the_wildcard_flags_are_derived_from_the_scope_not_sent_by_the_client():
    """`all_teams` and `scope_team_id` are two spellings of one fact and the
    schema checks they agree. Deriving them here means a client cannot send a
    pair that contradicts itself."""
    assert "body.scopeTeamId is None" in CREATE
    assert "body.scopeTargetId is None" in CREATE
    # `tier`, not `body.maxTier`: the ceiling is case-normalised before it is
    # stored, and `any_tier` has to be derived from the value that was written
    # rather than the one that arrived.
    assert "tier is None" in CREATE


def test_revoking_keeps_the_row():
    """Who could approve what, and until when, is a question an audit asks
    after the fact."""
    assert "revoked_at = NOW()" in REVOKE
    assert "DELETE FROM role_assignment" not in SRC


def test_expired_roles_are_not_listed_as_in_force():
    assert "valid_until IS NULL OR ra.valid_until > NOW()" in LIST


def test_the_listing_resolves_scopes_to_names():
    """A screen showing "team 7" makes the reader go and look it up."""
    assert "team_name" in LIST and "ts.alias" in LIST


# --- the tier is one word on both sides of the seam --------------------------


def test_the_tier_is_served_uppercase_like_every_other_tier():
    """`mapping.py` uppercases the tier on grants and on the queue. This route
    served the raw lowercase column, so the one screen that reads a ceiling
    matched `QH_TIER_WORD[r.maxTier]` against 'ro' and rendered nothing where a
    limit was in force."""
    assert '(r["max_tier"] or "").upper() or None' in SRC


def test_either_case_is_accepted_on_the_way_in():
    """The client speaks uppercase and the `tier` table is lowercase. Rejecting
    'RW' would mean the screen has to know which side of the seam it is on."""
    assert '.strip().lower() or None' in CREATE
    assert 'tier is not None and tier not in ("ro", "rw", "ddl")' in CREATE


def test_the_stored_tier_is_the_normalised_one_not_the_body():
    """`max_tier` is a foreign key into `tier`, whose names are lowercase. The
    uppercase the client sends would be refused by the FK, not coerced."""
    i = CREATE.index("INSERT INTO role_assignment")
    assert "body.maxTier" not in CREATE[i:], "the raw body tier reaches the insert"


# --- a ceiling that limits nothing is refused --------------------------------


def test_a_ceiling_is_refused_on_a_role_that_never_reads_it():
    """`can_approve` looks at admin and approver rows only. Stored on a granter
    it renders on the screen as a limit and enforces nothing — the shape of a
    permission bug that reviews clean."""
    assert "_ROLES_WITH_CEILING" in CREATE
    assert "nothing reads it on a" in CREATE


def test_the_ceiling_roles_are_the_ones_can_approve_actually_reads():
    """Two lists that must agree, in two files. If approving ever consults a
    third role, this is what fails."""
    import re as _re
    from pathlib import Path as _P
    acc = (_P(__file__).resolve().parent.parent / "src" / "queryhub"
           / "access.py").read_text(encoding="utf-8")
    reads = _re.search(r'roles\(principal_id\) if r\["role"\] in \(([^)]*)\)', acc)
    stores = _re.search(r'_ROLES_WITH_CEILING = \(([^)]*)\)', SRC)
    assert reads and stores
    assert set(_re.findall(r'"(\w+)"', reads.group(1))) == \
           set(_re.findall(r'"(\w+)"', stores.group(1)))


# --- the same authority is not stated twice ----------------------------------


def test_a_second_role_over_the_same_scope_is_refused():
    """`can_approve` admits on ANY covering row, so a duplicate differing only
    in its ceiling means the wider one wins while the narrower one reads, on
    screen, like a limit in force. The model treats a role as immutable —
    changing one is revoke plus create — which only holds if the same statement
    cannot be written twice."""
    assert "409" in CREATE and "Revoke it first" in CREATE
    assert "scope_team_id IS NOT DISTINCT FROM" in CREATE


def test_the_database_is_the_real_guarantee_not_the_lookup():
    """Two concurrent POSTs both pass a SELECT and both insert. The index is
    what makes that impossible; the lookup only buys a readable sentence."""
    from pathlib import Path as _P
    mig = (_P(__file__).resolve().parent.parent / "migrations"
           / "110_role_assignment_uniqueness.sql").read_text(encoding="utf-8")
    assert "CREATE UNIQUE INDEX IF NOT EXISTS role_assignment_live_uq" in mig
    assert "NULLS NOT DISTINCT" in mig
    assert "WHERE NOT is_deleted AND revoked_at IS NULL" in mig
    assert "role_assignment_live_uq" in CREATE      # the sentence names it


def test_revoked_rows_stay_outside_the_uniqueness():
    """Otherwise revoking and re-granting the same role — the only way to
    change one — would be refused by the row it just replaced."""
    from pathlib import Path as _P
    mig = (_P(__file__).resolve().parent.parent / "migrations"
           / "110_role_assignment_uniqueness.sql").read_text(encoding="utf-8")
    i = mig.index("role_assignment_live_uq")
    assert "revoked_at IS NULL" in mig[i:i + 400]


# --- what the screen cannot infer -------------------------------------------


def test_the_wildcards_are_sent_as_their_own_booleans():
    """"No team" and "every team" are opposite answers and a null id cannot
    tell them apart. On an authorization screen that difference is the scope."""
    for key in ('"allTeams"', '"allTargets"', '"anyTier"'):
        assert key in SRC, key
    assert "ra.all_teams" in LIST and "ra.any_tier" in LIST


def test_a_disabled_person_keeps_their_roles_and_the_screen_can_say_so():
    """Disabling stops somebody submitting; it revokes nothing. A leaver still
    holding approval authority is what this screen exists to surface."""
    assert '"enabled": bool(r["enabled"])' in SRC
    assert "p.enabled" in LIST
    assert "ORDER BY p.enabled DESC" in LIST


def test_the_listing_says_whether_these_rows_decide_anything_yet():
    """Until `access_model_v2` is on, `admins.can_approve` reads the old table
    and a scoped approver written here decides nothing. Staging the rows before
    the switch is deliberate; leaving that fact in a source comment is not — a
    screen showing a dormant approver is telling its reader something untrue
    about who can approve their requests."""
    assert '"enforced": teams.use_v2()' in LIST


# --- round (b): each refusal is told apart by code, not by its sentence -------


@pytest.mark.parametrize("code,what", [
    ("admin_scope", "an admin given a scope"),
    ("tier_scope", "a ceiling where nothing reads one"),
    ("no_account", "a person with no principal row"),
    ("no_team", "a team that does not exist"),
])
def test_every_refusal_carries_its_own_code(code, what):
    """The screen renders each refusal beside the field it is about, and the
    only thing it can tell them apart by is the code. Sharing `bad_request`
    across four meant every one of them landed in the footer instead.

    This codebase has already paid for matching on message text once: the
    duplicate wording did not match the client's duplicate regex, so a
    duplicate was shown as a confirm dialog and re-sent with `confirmed: true`
    (routes_queries.py, measured 2026-08-14)."""
    assert f'"{code}"' in CREATE, what


def test_the_duplicate_hands_back_the_row_id_as_a_field():
    """The revoke-and-recreate button is built on `roleId` and renders only
    when it arrives. The id is in the sentence too, but a client that had to
    parse it back out of prose is the failure above, again."""
    assert "roleId=dup[" in CREATE


# --- round (b): a disabled account holds no roles ----------------------------


def test_a_disabled_principal_holds_no_roles():
    """Legacy `admins.is_admin` filters `AND enabled = TRUE`; the new
    `roles()` did not, so the two models disagreed about an offboarded person
    — and `require_whitelisted` is `requesters.is_allowed(uid) or
    admins.is_admin(uid)`, so under v2 that person still got into QueryHub.

    Latent rather than live: measured 2026-09-07, one disabled principal and
    it holds nothing, which is why the fleet diff was empty either way. The
    shape appears the first time somebody is offboarded while holding a role,
    which is the case this whole column exists for."""
    import inspect

    from queryhub import access
    src = inspect.getsource(access.roles)
    assert "p.enabled" in src


def test_the_grant_resolvers_deliberately_do_not_filter_enabled():
    """Legacy `effective_grant_for_user` / `can_use_target` do not either —
    the whitelist is enforced at entry, not inside the grant resolver. Pushing
    the predicate into the shared `_ME` CTE would have fixed roles and broken
    the equivalence the whole cutover rests on."""
    import inspect

    from queryhub import access
    assert "p.enabled" not in inspect.getsource(access._covering)
    i = access.__dict__["_ME"]
    assert "enabled" not in i, "the shared CTE must stay neutral"
