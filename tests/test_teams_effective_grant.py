"""teams.effective_grant_for_user — the resolution every submission depends on.

CLAUDE.md: "Grants resolve per-submission via teams.effective_grant_for_user()."
core_submit.py calls it to decide whether a query may run and with which
credential. It had no direct test. tests/test_grant_cross_product.py exercises
it only indirectly, through effective_mode_for_database, and only for the
per-database cross-product bug — so the precedence rules, the union semantics,
and the admin bypass were all unverified.

The rules under test, from the docstring:

  * unrestricted principals (admins, bypass-team-grants requesters) get a
    synthetic ddl grant on EVERY target with allowed_databases=None. This is
    the widest possible answer, so it must be returned only for the right
    principals — and it must short-circuit before any lookup.
  * a user_target_grants row WINS over any team grant, even a more permissive
    one. A per-user override is how an admin narrows someone; if a team grant
    could raise it back up, the override would be decorative.
  * with no user row, team grants aggregate: most-permissive mode, UNION of
    allowed_databases, and a single unrestricted team (NULL or empty list)
    makes the whole result unrestricted.
  * no grant of any kind is None — not an empty dict, not a default tier.

The empty-list-means-unrestricted rule is the subtle one and it cuts both ways,
so it is pinned on the user path and the team path separately.
"""
import pytest

from queryhub import teams


@pytest.fixture
def db(monkeypatch):
    """Drive the two queries the function makes. `user` answers the
    user_target_grants fetch_one; `team` answers the team_target_grants
    fetch_all. Also default the principal to restricted, so a test that means
    to check the admin path has to say so."""
    box = {"user": None, "team": [], "unrestricted": False, "queries": []}

    def fake_fetch_one(sql, params=None):
        box["queries"].append(sql)
        u = box["user"]
        # The real query selects an `expired` flag alongside the grant
        # (migration 096). A stub that omits it is not a lighter fixture, it is
        # a different row than production returns — so default it here and let
        # a test that cares set it explicitly.
        if isinstance(u, dict) and "expired" not in u:
            u = {**u, "expired": False}
        return u

    def fake_fetch_all(sql, params=None):
        box["queries"].append(sql)
        return box["team"]

    monkeypatch.setattr(teams.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(teams.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(teams, "_is_unrestricted",
                        lambda pid: box["unrestricted"])
    return box


# ------------------------------------------------------- the admin bypass


def test_unrestricted_principal_gets_full_access_without_a_lookup(db):
    """The widest answer in the system. It must also short-circuit: if it fell
    through to the queries, an admin with no grant rows would come back None
    and lose access to their own tool."""
    db["unrestricted"] = True
    out = teams.effective_grant_for_user("U0ADMIN", 7)
    assert out == {"mode": "ddl", "allowed_databases": None,
                   "source": "admin_or_bypass"}
    assert db["queries"] == [], "answered before touching the database"


def test_restricted_principal_does_not_get_the_bypass(db):
    """The mirror of the above — the only thing separating a developer from ddl
    on every target is this flag."""
    db["unrestricted"] = False
    db["user"] = None
    db["team"] = []
    assert teams.effective_grant_for_user("U0DEV", 7) is None


# --------------------------------------------------- user grant precedence


def test_user_grant_wins_over_a_more_permissive_team_grant(db):
    """A per-user row is how an admin NARROWS someone. If the team grant could
    win, the override would be cosmetic and the narrowing silently undone."""
    db["user"] = {"mode": "ro", "allowed_databases": ["nova"]}
    db["team"] = [{"mode": "ddl", "allowed_databases": None}]
    out = teams.effective_grant_for_user("U0DEV", 7)
    assert out["mode"] == "ro"
    assert out["allowed_databases"] == {"nova"}
    assert out["source"] == "user"


def test_user_grant_with_no_database_list_means_every_database(db):
    db["user"] = {"mode": "rw", "allowed_databases": None}
    out = teams.effective_grant_for_user("U0DEV", 7)
    assert out["allowed_databases"] is None
    assert out["source"] == "user"


def test_user_grant_with_an_empty_database_list_also_means_every_database(db):
    """An empty array and NULL are treated the same. Worth pinning explicitly:
    reading `[]` as "no databases allowed" would be the safer-looking guess,
    and it is not what the code does — so a caller must not assume it."""
    db["user"] = {"mode": "rw", "allowed_databases": []}
    assert teams.effective_grant_for_user("U0DEV", 7)["allowed_databases"] is None


def test_only_active_user_grants_count(db):
    """The query filters revoked_at IS NULL, so a revoked row must look like no
    row and fall through to the team grants."""
    db["user"] = None
    db["team"] = [{"mode": "ro", "allowed_databases": ["nova"]}]
    out = teams.effective_grant_for_user("U0DEV", 7)
    assert out["source"] == "team"
    assert "revoked_at IS NULL" in db["queries"][0]


# ------------------------------------------------------ team aggregation


def test_team_grants_take_the_most_permissive_mode(db):
    db["team"] = [{"mode": "ro", "allowed_databases": ["a"]},
                  {"mode": "rw", "allowed_databases": ["a"]},
                  {"mode": "ro", "allowed_databases": ["a"]}]
    assert teams.effective_grant_for_user("U0DEV", 7)["mode"] == "rw"


def test_team_grants_union_their_database_lists(db):
    db["team"] = [{"mode": "ro", "allowed_databases": ["a", "b"]},
                  {"mode": "ro", "allowed_databases": ["b", "c"]}]
    out = teams.effective_grant_for_user("U0DEV", 7)
    assert out["allowed_databases"] == {"a", "b", "c"}


@pytest.mark.parametrize("unrestricted_row", [None, []])
def test_one_unrestricted_team_makes_the_whole_grant_unrestricted(
        db, unrestricted_row):
    """Membership in any team with full database access gives full access —
    intersecting instead would be wrong, and silently narrowing someone's real
    access is its own kind of bug."""
    db["team"] = [{"mode": "ro", "allowed_databases": ["a"]},
                  {"mode": "ro", "allowed_databases": unrestricted_row}]
    assert teams.effective_grant_for_user("U0DEV", 7)["allowed_databases"] is None


def test_no_grant_anywhere_is_none(db):
    """None, distinctly — a caller testing truthiness must not confuse "no
    access" with "access to nothing" or with a default tier."""
    db["user"] = None
    db["team"] = []
    assert teams.effective_grant_for_user("U0DEV", 7) is None


def test_the_mode_ordering_is_ro_then_rw_then_ddl(db):
    """_max_mode drives the credential the executor picks, so its ordering is
    an access decision. Check every pair rather than trusting the name."""
    assert teams._max_mode(["ro", "rw"]) == "rw"
    assert teams._max_mode(["rw", "ddl"]) == "ddl"
    assert teams._max_mode(["ro", "ddl"]) == "ddl"
    assert teams._max_mode(["ro"]) == "ro"
    # Unknown tiers rank 0, so they lose to everything and the result falls back
    # to 'ro' — fail-safe. Pinned because the alternative (an unrecognised value
    # sorting high, or propagating through as the answer) would hand out a
    # credential nobody chose. Empty input is the same story.
    assert teams._max_mode(["ro", "nonsense"]) == "ro"
    assert teams._max_mode(["nonsense"]) == "ro"
    assert teams._max_mode([]) == "ro"
    # ...but a real tier still beats an unknown one rather than being dragged
    # down by it.
    assert teams._max_mode(["ddl", "nonsense"]) == "ddl"


# ----------------------------------- the two thin wrappers over the resolver


def test_can_use_database_respects_the_resolved_scope(db):
    db["user"] = {"mode": "ro", "allowed_databases": ["nova"]}
    assert teams.can_use_database("U0DEV", 7, "nova") is True
    assert teams.can_use_database("U0DEV", 7, "other") is False


def test_can_use_database_is_false_without_any_grant(db):
    db["user"] = None
    db["team"] = []
    assert teams.can_use_database("U0DEV", 7, "nova") is False


def test_unrestricted_scope_allows_any_database_name(db):
    db["user"] = {"mode": "rw", "allowed_databases": None}
    assert teams.can_use_database("U0DEV", 7, "anything-at-all") is True


# ---------------------------------------------- can_use_target + visibility
#
# The visibility rules are documented in prose and differ per principal kind in
# a way that is easy to get backwards: an ADMIN sees disabled targets too (so a
# DBA can pick one for cleanup), while a BYPASS requester sees every ENABLED
# target but not hidden ones — "see everywhere", not "see hidden things".

@pytest.fixture
def principals(monkeypatch):
    """Control is_admin / bypasses_team_grants independently, since the whole
    point of these tests is that the two are NOT the same principal kind."""
    box = {"admin": False, "bypass": False, "rows": [], "one": None,
           "queries": []}
    monkeypatch.setattr(teams.admins, "is_admin", lambda pid: box["admin"])
    monkeypatch.setattr(teams.requesters, "bypasses_team_grants",
                        lambda pid: box["bypass"])

    def fetch_all(sql, params=None):
        box["queries"].append(" ".join(sql.split()))
        return box["rows"]

    def fetch_one(sql, params=None):
        box["queries"].append(" ".join(sql.split()))
        return box["one"]

    monkeypatch.setattr(teams.db, "fetch_all", fetch_all)
    monkeypatch.setattr(teams.db, "fetch_one", fetch_one)
    return box


@pytest.mark.parametrize("admin,bypass", [(True, False), (False, True)])
def test_unrestricted_principals_may_use_any_target(principals, admin, bypass):
    principals["admin"], principals["bypass"] = admin, bypass
    assert teams.can_use_target("U0X", 12345) is True
    assert principals["queries"] == [], "no lookup needed"


def test_restricted_principal_needs_a_grant_to_use_a_target(principals):
    principals["one"] = None
    assert teams.can_use_target("U0DEV", 7) is False
    principals["one"] = {"?column?": 1}
    assert teams.can_use_target("U0DEV", 7) is True


def test_can_use_target_ignores_revoked_user_grants(principals):
    """A revoked grant must not keep a target usable. The check lives in SQL,
    so assert the predicate is actually in the statement."""
    principals["one"] = None
    teams.can_use_target("U0DEV", 7)
    sql = principals["queries"][0]
    assert "revoked_at IS NULL" in sql
    # Both grant kinds are consulted — team OR per-user.
    assert "team_target_grants" in sql and "user_target_grants" in sql


def test_admin_sees_disabled_targets_too(principals):
    """The documented difference between admin and bypass. If the admin query
    filtered on enabled, a DBA could not select a disabled target for cleanup —
    which is the stated reason the distinction exists."""
    principals["admin"] = True
    principals["rows"] = []
    teams.list_targets_for_user("U0ADMIN")
    sql = principals["queries"][0]
    assert "FROM target_servers" in sql
    assert "WHERE enabled" not in sql, "admins must see disabled rows"
    assert "enabled DESC" in sql, "...but disabled ones sort last"


def test_bypass_requester_sees_only_enabled_targets(principals):
    """"See everywhere" is not "see hidden things"."""
    principals["admin"], principals["bypass"] = False, True
    principals["rows"] = []
    teams.list_targets_for_user("U0BYPASS")
    assert "enabled" in principals["queries"][0].lower()


def test_ordinary_user_listing_goes_through_their_grants(principals):
    principals["rows"] = []
    teams.list_targets_for_user("U0DEV")
    sql = principals["queries"][0]
    assert "team_target_grants" in sql or "user_target_grants" in sql, \
        "an ordinary user's target list must be grant-derived, not the catalog"


def test_has_any_grant_is_false_for_a_user_with_nothing(principals):
    principals["one"] = None
    assert teams.has_any_grant("U0DEV") is False
    principals["one"] = {"?column?": 1}
    assert teams.has_any_grant("U0DEV") is True


# ------------------------------------------------- expiry (migration 096)


def test_an_expired_user_grant_gives_no_access(db):
    """A dated grant that has passed its date is simply gone."""
    db["user"] = {"mode": "rw", "allowed_databases": None, "expired": True}
    db["team"] = []
    assert teams.effective_grant_for_user("U0DEV", 7) is None


def test_an_expired_user_override_does_not_fall_back_to_a_wider_team_grant(db):
    """The rule worth a test of its own.

    A user row is often written to NARROW what a team already allows — "this
    person, read-only on this target, until Friday". If expiry fell through to
    the team aggregate, Friday would arrive and hand them the team's `ddl`
    back: an expiry that INCREASES access. Expiry only ever removes.
    """
    db["user"] = {"mode": "ro", "allowed_databases": ["nova"], "expired": True}
    db["team"] = [{"mode": "ddl", "allowed_databases": None}]
    assert teams.effective_grant_for_user("U0DEV", 7) is None


def test_a_live_user_grant_is_unaffected(db):
    """The migration gave every existing row NULL, so the overwhelmingly common
    case is `expired=False` and nothing about the answer changes."""
    db["user"] = {"mode": "rw", "allowed_databases": None, "expired": False}
    db["team"] = [{"mode": "ro", "allowed_databases": ["x"]}]
    out = teams.effective_grant_for_user("U0DEV", 7)
    assert out["mode"] == "rw" and out["source"] == "user"


def test_team_expiry_is_filtered_in_sql_not_here(db):
    """The team aggregate has no per-row flag to check: expired team grants are
    excluded by the query itself, so what this pins is that the predicate is
    still IN the SQL the function issues."""
    db["user"] = None
    db["team"] = [{"mode": "ro", "allowed_databases": None}]
    teams.effective_grant_for_user("U0DEV", 7)
    team_sql = [q for q in db["queries"] if "team_target_grants" in q]
    assert team_sql, "the team aggregate did not run"
    assert "g.expires_at" in team_sql[-1], "team expiry is not enforced in SQL"
    assert "g.revoked_at" in team_sql[-1], "team revoke is not enforced in SQL"
