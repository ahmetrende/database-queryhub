"""On an Athena archive nobody but a super-admin holds, or is shown, more than RO.

The first half of the operator's archive rule (2026-10-01): "except
super-admins, everyone is at most RO". Two halves of its own:

* The engine refuses a write for EVERYONE. Athena is read-only here, and the
  refusal comes from the safety pass before any tier is compared, so a person
  with `bypass_team_grants` -- whose grant resolves to DDL on every target --
  is refused exactly like anyone else, and so is a super-admin.
* What the screens SAY. A fleet-wide grant resolves to DDL everywhere, and
  every screen that names a tier per target said DDL for the archive too: the
  web connection list and editor, the person and team effective-access views,
  the MCP connection list, and `/sql whoami`, `/sql roles` and `/sql teams` in
  Slack. They now say RO there for anybody but a super-admin
  (`engines.shown_tier`). No grant row changes.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from queryhub import (access, athena_exec, auto_approve, core_submit as cs, db, engines,
                           grants, lifecycle, query_safety, targets, teams)

BYPASS = "U0EXAMPLE002"    # reaches every target: DDL everywhere
SUPER = "U0EXAMPLE008"
READ = "SELECT count(*) FROM ledgers WHERE day = DATE '2026-09-01'"
WRITES = [
    "INSERT INTO ledgers SELECT * FROM ledgers",
    "UPDATE ledgers SET amount = 0 WHERE id = 1",
    "DELETE FROM ledgers WHERE id = 1",
    "CREATE TABLE ledgers_copy AS SELECT * FROM ledgers",
    "DROP TABLE ledgers",
    "ALTER TABLE ledgers ADD COLUMN note varchar",
]
DDL_EVERYWHERE = {"mode": "ddl", "allowed_databases": None, "source": "admin_or_bypass"}
SUPER_ROLE = {"role": "admin", "scope_team_id": None, "all_teams": True,
              "scope_target_id": None, "all_targets": True, "max_tier": None,
              "any_tier": True, "valid_until": None}


def _target(tid, engine, alias=None, engine_config=None):
    return targets.TargetServer(
        id=tid, alias=alias or f"example-{engine}", host=f"{engine}.example.test", port=443,
        default_database="ledger", username="reader", enabled=True, notes=None,
        engine=engine, engine_config=dict(engine_config or {}))


ARCHIVE = _target(40, "athena")
ARCHIVE_ON = _target(41, "athena", "example-athena-on", {"auto_approve": True})
LEDGER = _target(7, "postgres")
CLICKHOUSE = _target(9, "clickhouse")


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    """Nothing here may reach a database."""
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)


@pytest.fixture
def people(monkeypatch):
    """BYPASS holds no role; SUPER is a super-admin. Read through the real
    `is_super_admin`, from faked `access.roles`."""
    st = {"roles": {SUPER: [SUPER_ROLE]}}
    monkeypatch.setattr(access, "roles", lambda pid: st["roles"].get(pid, []))
    monkeypatch.setattr(auto_approve, "active_grants", lambda pid, at=None: [])
    return st


def _super(uid):
    return access.is_super_admin(uid)


# ---------------------------------------------------------------------------
# the engine refuses a write, for everyone
# ---------------------------------------------------------------------------

@pytest.fixture
def submit(monkeypatch, people):
    """validate_submission for a person whose grant resolves to DDL on every
    target, as `bypass_team_grants` does."""
    st = {"target": ARCHIVE}
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.requesters, "open_request_count", lambda uid: 0)
    monkeypatch.setattr(cs.admins, "is_super_admin", _super)
    monkeypatch.setattr(cs.targets, "get", lambda tid: st["target"])
    monkeypatch.setattr(cs.teams, "effective_grant_for_user", lambda uid, tid: DDL_EVERYWHERE)
    monkeypatch.setattr(cs.teams, "effective_mode_for_database", lambda *a: "ddl")
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: False)
    monkeypatch.setattr(cs.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(athena_exec, "config_of", lambda t: {})
    monkeypatch.setattr(athena_exec, "risk_hint", lambda *a, **k: None)
    monkeypatch.setattr(cs, "_archive_freshness", lambda t, c: None)

    def go(user, sql, justification="moving the month-end report"):
        return cs.validate_submission(
            user, "Ex", target_server_id=st["target"].id, database_name="ledger",
            query=sql, justification=justification, confirmed=True)
    st["go"] = go
    return st


@pytest.mark.parametrize("user", [BYPASS, SUPER], ids=["bypass", "super-admin"])
@pytest.mark.parametrize("sql", WRITES)
def test_a_write_on_an_archive_is_refused_for_everyone(submit, user, sql):
    out = submit["go"](user, sql)
    assert isinstance(out, cs.Rejection), out
    assert out.field == "query" and "read-only" in out.message


@pytest.mark.parametrize("sql", WRITES)
def test_it_is_the_engine_not_the_grant_that_refuses(submit, sql):
    """The same statement and the same DDL grant get through on PostgreSQL."""
    submit["target"] = LEDGER
    out = submit["go"](BYPASS, sql)
    assert isinstance(out, cs.Prepared), out
    assert out.required_mode in ("rw", "ddl")


def test_a_read_on_the_archive_is_still_a_read(submit):
    out = submit["go"](BYPASS, READ)
    assert isinstance(out, cs.Prepared) and out.required_mode == "ro"


@pytest.mark.parametrize("sql", WRITES)
def test_the_safety_pass_refuses_it_before_any_tier_even_unrestricted(sql):
    for unrestricted in (False, True):
        report = query_safety.analyze(sql, engine="athena", unrestricted=unrestricted)
        assert report.blocked and "read-only" in report.blockers[0]


# ---------------------------------------------------------------------------
# what a screen shows
# ---------------------------------------------------------------------------

def test_only_athena_has_a_ceiling():
    assert engines.spec("athena").tier_ceiling == "ro"
    for engine in ("postgres", "mssql", "clickhouse", None):
        assert engines.spec(engine).tier_ceiling is None


@pytest.mark.parametrize("tier,shown", [("ddl", "ro"), ("rw", "ro"), ("ro", "ro"),
                                        ("DDL", "RO"), ("RW", "RO"), (None, None)])
def test_an_archive_tier_is_shown_as_ro(tier, shown):
    assert engines.shown_tier("athena", tier) == shown


@pytest.mark.parametrize("engine", ["postgres", "mssql", "clickhouse", None])
def test_other_engines_show_the_tier_as_it_is(engine):
    for tier in ("ro", "rw", "ddl", "DDL"):
        assert engines.shown_tier(engine, tier) == tier


def test_a_super_admin_is_shown_what_they_hold():
    assert engines.shown_tier("athena", "ddl", True) == "ddl"
    assert engines.shown_tier("athena", "DDL", lambda: True) == "DDL"


def test_the_super_admin_question_is_asked_only_when_the_cap_would_change_it():
    asked = []

    def is_super():
        asked.append(1)
        return False
    assert engines.shown_tier("postgres", "ddl", is_super) == "ddl"
    assert engines.shown_tier("athena", "ro", is_super) == "ro"
    assert asked == []
    assert engines.shown_tier("athena", "rw", is_super) == "ro"
    assert asked == [1]


# --- the web connection list ---------------------------------------------------

@pytest.fixture
def connections(monkeypatch, people):
    from queryhub.web import routes_data as rd
    st = {"admin": False, "rd": rd}
    monkeypatch.setattr(rd.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rd.admins, "is_admin", lambda uid: st["admin"])
    monkeypatch.setattr(rd.admins, "is_super_admin", _super)
    fleet = [LEDGER, ARCHIVE, ARCHIVE_ON, CLICKHOUSE]
    monkeypatch.setattr(rd.targets, "list_enabled", lambda: fleet)
    monkeypatch.setattr(rd.targets, "list_all", lambda: fleet)
    monkeypatch.setattr(rd.db, "fetch_all", lambda sql, params=None: [])
    monkeypatch.setattr(rd.replicas, "enabled_by_primary", lambda ids: {})
    monkeypatch.setattr(rd.teams, "effective_grants_for_user",
                        lambda uid, ids: {i: dict(DDL_EVERYWHERE) for i in ids})
    monkeypatch.setattr(rd, "_catalog_databases_map", lambda ids: {i: ["ledger"] for i in ids})
    monkeypatch.setattr(rd, "_catalog_table_refs_map", lambda pairs: {})
    monkeypatch.setattr(rd, "_catalog_functions_map", lambda pairs: {})

    def tiers(user):
        return {c["id"]: c["databases"][0]["tier"]
                for c in rd.connections(claims={"sub": user})["connections"]}
    st["tiers"] = tiers
    return st


def test_the_connection_list_shows_ro_on_an_archive(connections):
    """The cap follows the engine, not the auto-approve setting: an archive that
    turns auto-approve on is no more writable."""
    assert connections["tiers"](BYPASS) == {
        "example-postgres": "DDL", "example-athena": "RO", "example-athena-on": "RO",
        "example-clickhouse": "DDL"}


def test_a_super_admin_sees_what_they_hold(connections):
    connections["admin"] = True
    assert connections["tiers"](SUPER)["example-athena"] == "DDL"


# --- the editor ------------------------------------------------------------------

@pytest.fixture
def classify(monkeypatch, people):
    from queryhub.web import routes_queries as rq
    st = {"target": ARCHIVE}
    monkeypatch.setattr(rq.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rq, "_target_by_alias", lambda alias: st["target"])
    monkeypatch.setattr(rq.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(rq.admins, "is_super_admin", _super)
    monkeypatch.setattr(teams, "effective_grant_for_user", lambda uid, tid: DDL_EVERYWHERE)
    monkeypatch.setattr(teams, "effective_mode_for_database", lambda uid, tid, d: "ddl")

    def go(user, sql=READ):
        body = rq.ClassifyIn(connectionId=st["target"].alias, databaseId="ledger", sql=sql)
        return rq.classify_query(body, claims={"sub": user})
    st["go"] = go
    return st


def test_the_editor_names_ro_as_the_granted_tier_on_an_archive(classify):
    got = classify["go"](BYPASS)
    assert got["grantedTier"] == "RO" and got["tierExceedsGrant"] is False
    blocked = classify["go"](BYPASS, WRITES[0])
    assert blocked["blocked"] is True and blocked["grantedTier"] == "RO"


def test_the_editor_is_unchanged_elsewhere_and_for_a_super_admin(classify):
    assert classify["go"](SUPER)["grantedTier"] == "DDL"
    classify["target"] = LEDGER
    assert classify["go"](BYPASS)["grantedTier"] == "DDL"


# --- the person view ---------------------------------------------------------------

def _res(tier, source="principal"):
    return {"tier": tier, "auto_tier": None, "source": source, "unrestricted": False,
            "db_role": None}


@pytest.fixture
def person(monkeypatch, people):
    from queryhub.web import routes_admin as ra, routes_data
    st = {"grants": {ARCHIVE.id: dict(DDL_EVERYWHERE), LEDGER.id: dict(DDL_EVERYWHERE)},
          "decided": {}, "ra": ra}
    monkeypatch.setattr(ra.targets, "list_all", lambda: [LEDGER, ARCHIVE])
    monkeypatch.setattr(teams, "effective_grants_for_user", lambda pid, ids: st["grants"])
    monkeypatch.setattr(routes_data, "_catalog_databases_map",
                        lambda ids: {i: ["ledger", "audit"] for i in ids})
    monkeypatch.setattr(access, "resolve_databases",
                        lambda pid, pairs: {p: st["decided"].get(p, (None, None)) for p in pairs})
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: [])
    monkeypatch.setattr(ra.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(auto_approve, "_team_waiver_applies", lambda *a: True)
    monkeypatch.setattr(grants, "authz", lambda pid: None)
    monkeypatch.setattr(ra.admins, "is_super_admin", _super)
    monkeypatch.setattr(ra, "_alias_of", lambda tid: None)
    return st


def _by_connection(rows):
    out: dict = {}
    for r in rows:
        out.setdefault(r["connectionId"], []).append(r["tier"])
    return out


@pytest.mark.parametrize("view", ["_effective_access_v2", "_effective_access_legacy"])
def test_a_fleet_wide_reader_reads_ro_on_the_archive(person, view):
    out, _auto, _admin = getattr(person["ra"], view)(BYPASS)
    assert _by_connection(out) == {"example-postgres": ["DDL"], "example-athena": ["RO"]}


@pytest.mark.parametrize("view", ["_effective_access_v2", "_effective_access_legacy"])
def test_a_super_admin_reads_what_they_hold(person, view):
    out, _auto, _admin = getattr(person["ra"], view)(SUPER)
    assert _by_connection(out)["example-athena"] == ["DDL"]


def test_an_archive_is_one_ro_row_not_an_rw_row_and_an_ro_row(person):
    """Capped per database before the rows are grouped, so a personal RW grant
    on one archive database and a team's RO on another read as the one answer
    they amount to there."""
    person["grants"] = {ARCHIVE.id: {"mode": "rw", "allowed_databases": None, "source": "user"}}
    person["decided"] = {(ARCHIVE.id, "ledger"): (_res("rw"), None),
                         (ARCHIVE.id, "audit"): (_res("ro"), None)}
    [row] = person["ra"]._effective_access_v2(BYPASS)[0]
    assert row["tier"] == "RO" and row["mixedTiers"] is False
    assert [d["tier"] for d in row["perDatabase"]] == ["RO", "RO"]


# --- the team view -------------------------------------------------------------------

@pytest.fixture
def team(monkeypatch, people):
    from queryhub.web import routes_admin as ra
    rows = [{"target_id": ARCHIVE.id, "database_name": "ledger", "all_databases": False,
             "tier": "rw", "auto_approve": False, "valid_until": None},
            {"target_id": LEDGER.id, "database_name": "ledger", "all_databases": False,
             "tier": "rw", "auto_approve": False, "valid_until": None}]
    overrides = {ARCHIVE.id: [
        {"handle": BYPASS, "name": "Example", "tier": "DDL", "databases": ["ledger"],
         "expired": False, "expiresAt": None},
        {"handle": SUPER, "name": "Example", "tier": "DDL", "databases": ["ledger"],
         "expired": False, "expiresAt": None}]}
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, a: SUPER)
    monkeypatch.setattr(ra, "_team_for_write",
                        lambda tid: {"id": tid, "name": "team", "display_name": None, "source": None})
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: True)
    monkeypatch.setattr(ra, "_team_rows_v2", lambda tid: (rows, [], []))
    monkeypatch.setattr(ra.targets, "list_all", lambda: [LEDGER, ARCHIVE])
    monkeypatch.setattr(ra, "_super_approvers", lambda: 0)
    monkeypatch.setattr(ra, "_overrides_v2", lambda members, team_dbs: overrides)
    monkeypatch.setattr(ra.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(ra.db, "transaction", lambda: contextlib.nullcontext())
    monkeypatch.setattr(ra.audit, "log_in", lambda *a, **k: None)
    monkeypatch.setattr(ra.admins, "is_super_admin", _super)
    return ra


def test_a_teams_archive_grant_reads_ro_and_so_do_its_members_own(team):
    got = {a["connectionId"]: a for a in team.admin_team_effective_access(7, claims={})["access"]}
    archive, ledger = got["example-athena"], got["example-postgres"]
    assert archive["tier"] == "RO" and [d["tier"] for d in archive["perDatabase"]] == ["RO"]
    assert ledger["tier"] == "RW"
    shown = {o["handle"]: o["tier"] for o in archive["overriddenFor"]}
    assert shown == {BYPASS: "RO", SUPER: "DDL"}


# --- the MCP connection list -------------------------------------------------------------

def test_the_mcp_connection_list_shows_ro_on_an_archive(monkeypatch, people):
    from queryhub.mcp_server import policy, tools
    monkeypatch.setattr(tools, "_me", lambda: BYPASS)
    monkeypatch.setattr(tools.targets, "list_enabled", lambda: [LEDGER, ARCHIVE])
    monkeypatch.setattr(tools.teams, "effective_grant_for_user", lambda uid, tid: DDL_EVERYWHERE)
    monkeypatch.setattr(tools.admins, "is_super_admin", _super)
    monkeypatch.setattr(policy, "max_tier", lambda: "ro")
    got = {c["connection"]: c["tier"] for c in tools.list_connections()["connections"]}
    assert got == {"example-postgres": "DDL", "example-athena": "RO"}
    monkeypatch.setattr(tools, "_me", lambda: SUPER)
    assert tools.list_connections()["connections"][1]["tier"] == "DDL"


# --- Slack ---------------------------------------------------------------------------------

@pytest.fixture
def slack(monkeypatch, people):
    from queryhub.slack_app import subcommands as sc
    said = []
    monkeypatch.setattr(sc, "_respond", lambda respond, text: said.append(text))
    monkeypatch.setattr(sc.targets_mod, "list_all", lambda: [LEDGER, ARCHIVE])
    monkeypatch.setattr(sc.admins, "is_super_admin", _super)
    return SimpleNamespace(sc=sc, said=said)


def _who(uid, grants_):
    return {"slack_user_id": uid, "name": "Example", "email": None, "is_admin": False,
            "is_bypass": True, "admin_max_tier": None, "admin_scope_team_ids": None,
            "admin_scope_target_ids": None, "teams": [], "user_grants": grants_}


GRANT_TEXTS = ["*(ddl)", "example-athena(ddl)", "example-athena/ledger(rw)",
               "example-postgres(rw)"]


def test_whoami_shows_an_archive_grant_as_ro(monkeypatch, slack):
    rows = {BYPASS: _who(BYPASS, GRANT_TEXTS), SUPER: _who(SUPER, GRANT_TEXTS)}
    monkeypatch.setattr(slack.sc.db, "fetch_one", lambda sql, params=None: rows[params[0]])
    monkeypatch.setattr(slack.sc.db, "fetch_all", lambda sql, params=None: [])
    slack.sc._handle_whoami(BYPASS, "", None, None, {})
    assert ("User grants : *(ddl), example-athena(ro), example-athena/ledger(ro), "
            "example-postgres(rw)") in slack.said[0]
    slack.sc._handle_whoami(SUPER, "", None, None, {})
    assert ", ".join(GRANT_TEXTS) in slack.said[1]


def test_the_roles_list_shows_an_archive_grant_as_ro(monkeypatch, slack):
    monkeypatch.setattr(slack.sc.db, "fetch_all", lambda sql, params=None: [
        _who(BYPASS, GRANT_TEXTS), _who(SUPER, GRANT_TEXTS)])
    slack.sc._handle_roles(SUPER, "", None, None, {})
    [text] = slack.said
    assert text.count("example-athena(ro),example-athena/ledger(ro)") == 1
    assert text.count("example-athena(ddl),example-athena/ledger(rw)") == 1


def test_a_teams_archive_grant_reads_ro_in_slack(monkeypatch, slack):
    monkeypatch.setattr(slack.sc.teams, "team_detail", lambda name: {
        "team": {"name": "team", "description": None,
                 "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc)},
        "members": [],
        "grants": [{"alias": "example-athena", "mode": "rw", "allowed_databases": None,
                    "target_role": None, "engine": "athena"},
                   {"alias": "example-postgres", "mode": "rw", "allowed_databases": None,
                    "target_role": None, "engine": "postgres"},
                   {"alias": "every target", "mode": "ddl", "allowed_databases": None,
                    "target_role": None, "engine": None}]})
    slack.sc._handle_one_team("team", lambda *a, **k: None)
    [text] = slack.said
    assert "example-athena               [ RO]" in text
    assert "example-postgres             [ RW]" in text
    assert "every target                 [DDL]" in text


def test_team_detail_carries_each_grants_engine():
    import inspect
    src = inspect.getsource(teams.team_detail)
    assert src.count("ts.engine") == 2, "both models' bodies"
