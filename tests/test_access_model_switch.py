"""The switch between the two authorization models.

`teams.py` and `admins.py` keep their signatures and grow a second body each:
the tables they have always read, and a delegation to `access.py`. About 140
call sites across ten modules are untouched, because a rewrite of that size is
a rewrite with a missed call site in it — and a missed one in authorization is
a person who can suddenly reach something, or suddenly cannot.

What these tests protect is the shape of that arrangement, not the answers.
Equivalence of the answers is proved by capturing all 6930 access and 7740
approval answers from both sides and diffing them, which is empty. Here: that
every public function actually has the second body, that both modules read one
key through one helper, and that the flag defaults to off.
"""
import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEAMS = (ROOT / "src" / "queryhub" / "teams.py").read_text(encoding="utf-8")
ADMINS = (ROOT / "src" / "queryhub" / "admins.py").read_text(encoding="utf-8")
ACCESS = (ROOT / "src" / "queryhub" / "access.py").read_text(encoding="utf-8")
MIG = (ROOT / "migrations" / "107_access_model_v2_switch.sql").read_text(encoding="utf-8")


def _body(src: str, name: str) -> str:
    m = re.search(rf"\ndef {name}\(.*?(?=\ndef |\nclass |\Z)", src, re.S)
    assert m, f"{name} is gone"
    return m.group(0)


# --- every decision has a second body ---------------------------------------


@pytest.mark.parametrize("name", [
    "_is_unrestricted", "list_targets_for_user", "search_targets_for_user",
    "can_use_target", "can_use_database", "allowed_databases_for_user",
    "expired_grant_at", "effective_grant_for_user", "effective_grants_for_user",
    "effective_mode_for_database", "has_any_grant"])
def test_every_resolver_function_can_delegate(name):
    body = _body(TEAMS, name)
    assert "if use_v2():" in body, f"teams.{name} never reaches the new model"
    assert "access." in body, f"teams.{name} claims to delegate but does not"


@pytest.mark.parametrize("name", ["list_active", "is_admin", "is_super_admin",
                                  "can_approve"])
def test_every_admin_decision_can_delegate(name):
    body = _body(ADMINS, name)
    assert "if _v2():" in body, f"admins.{name} never reaches the new model"
    assert "access." in body


def test_the_delegation_is_the_first_thing_each_function_does():
    """After any legacy query has run, the answer is already the old one. A
    check placed lower down would read the old tables and then discard the
    result, which is slower and — where the two disagree — wrong."""
    for name in ("can_use_target", "has_any_grant", "effective_mode_for_database"):
        body = _body(TEAMS, name)
        after_doc = body.split('"""')[2] if body.count('"""') >= 2 else body
        first = next(ln for ln in after_doc.splitlines() if ln.strip())
        assert "use_v2()" in first, f"teams.{name} delegates too late: {first!r}"


# --- one key, one helper ----------------------------------------------------


def test_both_modules_read_the_switch_through_one_helper():
    """Two readings of one key can land on different sides mid-request, which
    would be a query authorized by one model and approved by the other."""
    assert TEAMS.count('get_setting("access_model_v2"') == 1
    assert 'get_setting("access_model_v2"' not in ADMINS
    assert "from .teams import use_v2" in ADMINS


def test_the_switch_is_read_per_call_not_captured_at_import():
    """`bot_config` is runtime-effective everywhere else in this product, and
    the rollback for the whole cutover is turning this key back — which a value
    captured at import would ignore until a restart."""
    from queryhub import teams
    src = inspect.getsource(teams.use_v2)
    assert "get_setting" in src
    assert not re.search(r"^_USE_V2\s*=", TEAMS, re.M)


def test_the_key_is_seeded_off():
    assert "'access_model_v2', 'off'" in MIG
    assert "ON CONFLICT (key) DO NOTHING" in MIG


def test_the_default_is_off_in_code_too():
    """If the row were ever missing, the absent case must be the old model."""
    from queryhub import teams
    assert '"off"' in inspect.getsource(teams.use_v2)


# --- the translation lives in one place -------------------------------------


def test_the_legacy_vocabulary_is_produced_by_one_adapter():
    """`admin_or_bypass` is a word only the old model uses. Every caller that
    prints or branches on it keeps working because one function speaks it."""
    assert ACCESS.count('source = "admin_or_bypass"') == 1
    assert "def legacy_shape(" in ACCESS
    assert TEAMS.count("access.legacy_shape(") == 2   # single and batch


def test_the_old_bodies_are_still_there():
    """They are the rollback. Deleting them is a separate change, made once the
    flag has been on long enough to trust, not a tidy-up done in passing."""
    assert "FROM team_target_grants" in TEAMS
    assert "FROM user_target_grants" in TEAMS
    assert "FROM admins" in ADMINS


# --- the delegation actually runs -------------------------------------------
#
# The tests above read the source; these execute it. With the flag on, each
# public function must call its counterpart in `access` and hand back what it
# returns, translated where the vocabularies differ — and must not touch the
# old tables on the way, which a stub that raises is the simplest way to prove.


class _Spy:
    """Stands in for `access`, recording what it was asked."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls: list[str] = []

    def __getattr__(self, name):
        def call(*a, **k):
            self.calls.append(name)
            if name not in self.answers:
                raise AssertionError(f"unexpected call to access.{name}")
            return self.answers[name]
        return call


@pytest.fixture
def v2(monkeypatch):
    from queryhub import teams
    monkeypatch.setattr(teams, "use_v2", lambda: True)

    def no_database(*a, **k):
        raise AssertionError("the legacy tables were read with the flag on")

    monkeypatch.setattr(teams.db, "fetch_one", no_database)
    monkeypatch.setattr(teams.db, "fetch_all", no_database)
    return teams


def _spy(monkeypatch, teams, **answers):
    spy = _Spy(**answers)
    monkeypatch.setattr(teams, "access", spy)
    return spy


RESOLVED = {"tier": "rw", "auto_tier": "ro", "source": "principal",
            "unrestricted": False, "db_role": None, "databases": {"app"}}


def test_effective_grant_for_user_returns_the_old_shape(v2, monkeypatch):
    from queryhub import access as real
    _spy(monkeypatch, v2, resolve_target=RESOLVED, legacy_shape=real.legacy_shape(RESOLVED))
    got = v2.effective_grant_for_user("U1", 5)
    assert got == {"mode": "rw", "allowed_databases": {"app"}, "source": "user"}


def test_effective_mode_for_database_returns_the_tier(v2, monkeypatch):
    _spy(monkeypatch, v2, resolve=RESOLVED)
    assert v2.effective_mode_for_database("U1", 5, "app") == "rw"


def test_effective_mode_for_database_is_none_without_a_grant(v2, monkeypatch):
    _spy(monkeypatch, v2, resolve=None)
    assert v2.effective_mode_for_database("U1", 5, "app") is None


def test_allowed_databases_is_an_empty_set_without_a_grant(v2, monkeypatch):
    """None means 'no restriction' and an empty set means 'nothing'. Returning
    the wrong one of those opens every database on the target."""
    _spy(monkeypatch, v2, resolve_target=None)
    assert v2.allowed_databases_for_user("U1", 5) == set()


def test_allowed_databases_passes_the_unrestricted_none_through(v2, monkeypatch):
    _spy(monkeypatch, v2, resolve_target={**RESOLVED, "databases": None})
    assert v2.allowed_databases_for_user("U1", 5) is None


@pytest.mark.parametrize("fn,args,answers,expected", [
    ("can_use_target", ("U1", 5), {"can_use_target": True}, True),
    ("can_use_database", ("U1", 5, "app"), {"can_use_database": False}, False),
    ("has_any_grant", ("U1",), {"has_any_grant": True}, True),
    ("expired_grant_at", ("U1", 5), {"expired_grant_at": None}, None),
    ("list_targets_for_user", ("U1",), {"visible_targets": []}, []),
    ("search_targets_for_user", ("U1", "exc"), {"search_visible_targets": []}, []),
])
def test_the_simple_delegations_pass_through(v2, monkeypatch, fn, args,
                                             answers, expected):
    _spy(monkeypatch, v2, **answers)
    assert getattr(v2, fn)(*args) == expected


def test_the_batch_shapes_every_entry(v2, monkeypatch):
    from queryhub import access as real
    spy = _Spy(resolve_many={5: RESOLVED, 6: None})
    spy.answers["legacy_shape"] = None
    monkeypatch.setattr(v2, "access", spy)
    # legacy_shape is the real one: the translation is what is under test
    monkeypatch.setattr(spy, "answers", {**spy.answers})
    monkeypatch.setattr(v2.access, "legacy_shape", real.legacy_shape, raising=False)
    got = v2.effective_grants_for_user("U1", [5, 6])
    assert got[5]["mode"] == "rw" and got[6] is None


def test_unrestricted_asks_the_new_model_for_both_halves(v2, monkeypatch):
    """An admin and a fleet-wide grant are two different rows now."""
    _spy(monkeypatch, v2, is_admin=False, has_fleet_wide_grant=True)
    assert v2._is_unrestricted("U1") is True


def test_the_admin_module_delegates_too(monkeypatch):
    from queryhub import admins
    monkeypatch.setattr(admins, "_v2", lambda: True)

    def no_database(*a, **k):
        raise AssertionError("the admins table was read with the flag on")

    monkeypatch.setattr(admins.db, "fetch_one", no_database)
    monkeypatch.setattr(admins.db, "fetch_all", no_database)
    spy = _Spy(is_admin=True, is_super_admin=False, can_approve=True,
               list_admins=[{"slack_user_id": "U1"}])
    monkeypatch.setattr(admins, "access", spy)
    assert admins.is_admin("U1") is True
    assert admins.is_super_admin("U1") is False
    assert admins.can_approve("U1", {"required_tier": "ro"}) is True
    assert admins.list_active() == [{"slack_user_id": "U1"}]
    assert set(spy.calls) == {"is_admin", "is_super_admin", "can_approve",
                              "list_admins"}


# --- the write paths have not moved yet -------------------------------------
#
# The flag switches READS. Every write path still targets the old tables, so
# while it is on, a grant somebody issues has no effect until the copy runs
# again — and the person refused has no way to tell why. Two guards say so, and
# both are scaffolding to delete when the write paths move.


def test_a_grant_reaches_whichever_model_owns_the_team():
    """This used to assert that NO grant is ever written to the new tables —
    writes went to the legacy ones and the mirror projected them, so a direct
    write would have sat beside a mirrored one describing the same access
    twice.

    The pod cutover retired that premise rather than broke it. A pod team has
    no `teams` row for `team_target_grants.team_id` to reference, so its
    grants have nowhere else to go; the legacy table is empty and the branch
    that wrote to it could not grant a pod anything at all.

    Both paths are kept and chosen by the switch, which is what makes the
    direct write safe: the mirror only ever touches rows carrying
    `mirrored_from`, so a row written here is invisible to it and cannot be
    revoked out from under an admin.
    """
    src = (ROOT / "src" / "queryhub" / "web"
           / "routes_admin.py").read_text(encoding="utf-8")
    i = src.index('if stype == "team":')
    body = src[i:i + 3200]
    assert "teams_mod.use_v2()" in body, "the branch must follow the switch"
    assert "INSERT INTO access_grant" in body
    assert "INSERT INTO team_target_grants" in body

    mig = (ROOT / "migrations"
           / "109_mirror_legacy_writes.sql").read_text(encoding="utf-8")
    k = mig.index("UPDATE access_grant g SET revoked_at")
    assert "mirrored_from" in mig[k:k + 400], (
        "the mirror must scope its revokes to its own rows, or a grant written "
        "directly would be taken away on the next legacy write")

def test_the_roles_endpoint_writes_rows_the_mirror_will_not_touch():
    """`mirrored_from` left NULL is what keeps the mirror's revoke-what-has-no-
    source pass away from a row a person wrote."""
    routes = (ROOT / "src" / "queryhub" / "web"
              / "routes_admin.py").read_text(encoding="utf-8")
    i = routes.index("INSERT INTO role_assignment")
    stmt = routes[i:i + 900]
    assert "mirrored_from" not in stmt


def test_the_mirror_is_what_keeps_the_two_models_together():
    """The startup warning said reads and writes were on different models.
    Migration 109 is the answer to it, so the warning has to stop claiming a
    grant will not take effect."""
    from queryhub import access
    import inspect
    warning = inspect.getsource(access._log_v2_warning)
    assert "mirror" in warning.lower()


def test_the_copy_script_gates_the_flag_on_drift():
    """The wording moved on 2026-09-08 — the check compares the two models'
    grant SETS now, rather than asking whether a legacy row was timestamped
    after the newest mirrored one. Assert the property, not the sentence."""
    src = (ROOT / "scripts" / "copy_access_model.py").read_text(encoding="utf-8")
    assert "differ between the two models" in src
    assert "access_model_v2" in src


def test_a_service_that_boots_with_the_flag_on_says_so():
    from queryhub import access
    assert callable(access.warn_if_access_model_v2)
    for entry in ("main.py", "web/app.py"):
        text = (ROOT / "src" / "queryhub" / entry).read_text(encoding="utf-8")
        assert "warn_if_access_model_v2()" in text, entry


def test_the_warning_never_stops_a_service_booting(monkeypatch):
    """A config read that raises must not be the reason the bot will not
    start. The warning is scaffolding; the service is not."""
    from queryhub import access, config

    def boom(*a, **k):
        raise RuntimeError("no database")

    monkeypatch.setattr(config, "get_setting", boom)
    access.warn_if_access_model_v2()      # must not raise


def test_the_warning_is_silent_when_the_flag_is_off(monkeypatch):
    from queryhub import access, config
    said = []
    monkeypatch.setattr(config, "get_setting", lambda *a, **k: "off")
    monkeypatch.setattr(access, "_log_v2_warning", lambda: said.append(1))
    access.warn_if_access_model_v2()
    assert said == []


def test_the_warning_fires_when_the_flag_is_on(monkeypatch):
    from queryhub import access, config
    said = []
    monkeypatch.setattr(config, "get_setting", lambda *a, **k: "on")
    monkeypatch.setattr(access, "_log_v2_warning", lambda: said.append(1))
    access.warn_if_access_model_v2()
    assert said == [1]


# --- the reads that were still pinned to the legacy tables -------------------
#
# Added 2026-09-08, while sweeping for reads that would not follow the flag.
# Most of what turned up was fine: the team CRUD in routes_admin.py reads what
# it writes, and `_scope_admits` is only ever reached from `can_approve`'s
# legacy body. Three were not.

EXECUTOR = (ROOT / "src" / "queryhub" / "executor.py").read_text(encoding="utf-8")
SUBCMD = (ROOT / "src" / "queryhub" / "slack_app"
          / "subcommands.py").read_text(encoding="utf-8")
MIG112 = (ROOT / "migrations"
          / "112_team_summary_excludes_revoked.sql").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["list_team_summaries", "team_detail"])
def test_the_slack_team_views_follow_the_switch(name):
    """They read what the mirror keeps equal, so reading legacy is correct
    today and wrong the day writes move — correctness that expires without
    failing. They are also where the two models genuinely differ: the new one
    carries teams from an org import that the legacy table never heard of."""
    body = _body(TEAMS, name)
    assert "if use_v2():" in body
    assert "FROM team " in body or "FROM team\n" in body


def test_the_slack_team_views_go_through_teams_py():
    """The point of the two functions above is that nothing else reads those
    tables by hand."""
    assert "teams.list_team_summaries()" in SUBCMD
    assert "teams.team_detail(" in SUBCMD
    assert "FROM v_team_summary" not in SUBCMD
    assert "FROM team_target_grants" not in SUBCMD
    assert "FROM team_members" not in SUBCMD


def test_a_revoked_team_grant_is_not_listed_as_access():
    """The detail view listed every row in `team_target_grants`, tombstones
    included, and `v_team_summary` counted them the same way. Latent — there
    are no revoked team grants yet — and it stops being latent the first time
    one is revoked, on the screen whose whole job is to say who can reach
    what."""
    body = _body(TEAMS, "team_detail")
    assert body.count("revoked_at IS NULL") >= 2, "both bodies must filter"
    assert "g.revoked_at IS NULL" in MIG112
    assert "CREATE OR REPLACE VIEW v_team_summary" in MIG112


def test_the_executors_database_role_lookup_follows_the_switch():
    """`SET LOCAL ROLE <role>` before the query runs. `target_role` and its
    successor `db_role` are NULL on every row in both models, so both branches
    answer None today — but a role configured after the cutover would have
    been silently ignored, and the query then runs as the bot's login user
    instead of the role the grant named. Permissive, and invisible."""
    body = _body(EXECUTOR, "_team_role_for")
    assert "teams.use_v2()" in body
    assert "access.resolve_target(" in body
    assert "db_role" in body


def test_the_team_description_survives_the_cutover():
    """`teams` has a description and `team` did not, so every one of them
    would have vanished at the flip with nothing failing. One of the six is
    operational documentation of what the team is for."""
    mig = (ROOT / "migrations" / "111_team_description.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS description" in mig
    assert "UPDATE team t SET description = o.description" in mig


def test_the_mirror_notices_a_description_only_edit():
    """Its UPDATE was guarded on the name alone, so changing just the
    description left the projection stale until something else about that team
    changed."""
    mig = (ROOT / "migrations" / "111_team_description.sql").read_text(encoding="utf-8")
    i = mig.index("UPDATE team t SET display_name")
    guard = mig[i:i + 400]
    assert "t.description IS DISTINCT FROM o.description" in guard


# --- the fan-out had to learn about scoped approvers ------------------------


def test_the_request_fan_out_asks_a_different_question_than_list_active():
    """`list_active()` answers "who administers QueryHub", and four callers
    mean exactly that — the Admin scopes screen, the IdP drift report (which
    compares `source == 'permanent'` against what the panel believes), service
    notices, and CSV-import approvals, gated on `is_admin` so a scoped
    approver could not act on one anyway.

    Widening it would have put approvers into the panel's drift report as
    admins the IdP does not know about. So the fan-out gets its own function
    and the other four are untouched."""
    assert "def notify_list(request: dict)" in ADMINS
    assert "admins.notify_list(row)" in (
        ROOT / "src" / "queryhub" / "core_submit.py").read_text(encoding="utf-8")


def test_a_scoped_approver_was_never_told_a_request_was_waiting():
    """The gap this closes. `access.list_admins` filters `role = 'admin'`, so
    the one person the scoped-approver feature exists for — a pod lead who
    approves their own team — never appeared in the list the DM fan-out walks,
    and learned nothing about a request they could approve."""
    assert "ra.role = 'approver'" in _body(ADMINS, "notify_list")
    assert "ra.role = 'admin'" in _body(ACCESS, "list_admins")


def test_an_approver_is_listed_only_in_scope_and_an_admin_always():
    """Deliberately different. An administrator seeing traffic they cannot
    approve is transparency; a pod lead DM'd every request on the fleet mutes
    the bot within a day, and then the ones they CAN approve are lost too."""
    body = _body(ADMINS, "notify_list")
    assert "can_approve(uid, request)" in body
    assert "people = list_active()" in body      # admins, unfiltered


def test_the_scope_test_happens_once_so_both_consumers_agree():
    """`dispatch_and_notify` hands one list to the Slack fan-out and to the
    notification_outbox row. If the scope test ran in each, the two could name
    different people — the exact failure the single capture exists to stop."""
    assert "def notify_list(request: dict)" in ADMINS
    src = (ROOT / "src" / "queryhub" / "core_submit.py").read_text(encoding="utf-8")
    assert src.count("admins.notify_list(") == 1
    assert "admins.list_active()" not in src


def test_it_is_a_no_op_while_the_flag_is_off():
    """Same list as before the change, so nothing moves until the cutover."""
    body = _body(ADMINS, "notify_list")
    i = body.index("people = list_active()")
    assert "if not _v2():" in body[i:i + 120]


def test_the_bundle_fan_out_is_deliberately_left_alone():
    """`notify_admins_bundle` offers `act_bundle_approve_all` — one click for
    every item in the bundle. Listing a scoped approver there would hand them
    a button that approves items outside their scope, so widening it needs a
    per-item scope model first. Left as admins-only, on purpose."""
    notif = (ROOT / "src" / "queryhub" / "slack_app"
             / "notifications.py").read_text(encoding="utf-8")
    i = notif.index("def notify_admins_bundle")
    assert "admins.list_active()" in notif[i:i + 1200]
    assert "ACTION_BUNDLE_APPROVE_ALL" in notif


def test_the_web_teams_screen_follows_the_switch_too():
    """It read the legacy `teams` table directly, on the reasoning that the
    team CRUD beside it writes there too — read what you write. That held
    until the company moved to the pod structure: the legacy rows went, the
    teams now live only in the nine-table model, and the screen went from six
    teams to NONE while `/sql teams` showed thirteen. Two surfaces, one
    question, opposite answers — and the empty one is the screen an admin
    manages access from."""
    src = (ROOT / "src" / "queryhub" / "web"
           / "routes_admin.py").read_text(encoding="utf-8")
    i = src.index("def _teams_payload()")
    body = src[i:i + 2600]
    assert "teams_mod.use_v2()" in body
    assert "FROM team t WHERE NOT t.is_deleted" in body


def test_a_synced_team_is_marked_on_that_screen():
    """Renaming one there would be undone by the next sync run, so the screen
    has to be able to say which ones it does not own."""
    src = (ROOT / "src" / "queryhub" / "web"
           / "routes_admin.py").read_text(encoding="utf-8")
    assert '"syncedFrom"' in src


def test_a_team_grant_can_reach_the_new_model():
    """Under the new model it CANNOT go to the legacy table: a pod team has no
    `teams` row for `team_target_grants.team_id` to reference. After the pod
    cutover that table is empty, so this branch could not grant a team
    anything at all — the 27 grants the cutover wrote went straight to
    `access_grant` and there was no supported path to the 28th."""
    src = (ROOT / "src" / "queryhub" / "web"
           / "routes_admin.py").read_text(encoding="utf-8")
    i = src.index('if stype == "team":')
    body = src[i:i + 3200]
    assert "teams_mod.use_v2()" in body
    assert "INSERT INTO access_grant" in body
    assert "INSERT INTO team_target_grants" in body        # the legacy path stays


def test_changing_a_team_grants_tier_is_revoke_then_insert():
    """A row is immutable, and `access_grant_live_uq` would otherwise hold the
    old tier and the new one at once — the resolver takes the more permissive
    of the two, so an admin narrowing a grant would have widened it."""
    src = (ROOT / "src" / "queryhub" / "web"
           / "routes_admin.py").read_text(encoding="utf-8")
    i = src.index('if stype == "team":')
    body = src[i:i + 3200]
    j = body.index("teams_mod.use_v2()")
    assert "UPDATE access_grant SET revoked_at = NOW()" in body[j:j + 700]


def test_a_team_is_findable_by_the_name_the_screen_shows():
    """A pod's code is `team-a` and what everyone calls it is `Team A`.
    An admin typing what the screen shows should not get "no such team"."""
    src = (ROOT / "src" / "queryhub" / "web"
           / "routes_admin.py").read_text(encoding="utf-8")
    i = src.index("def _resolve_team(")
    body = src[i:i + 1200]
    assert "lower(display_name) = lower(%s)" in body
