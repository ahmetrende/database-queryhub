"""The DBA level: a gate between "any admin" and "super-admin only".

PLA-1592 asked to open four admin routes to a DBA: the kill switch (U4a), the
effective access of a person (U13), the connection list and the test of a
stored credential (U14). The card proposed the `review` level. That level
passes ANY `admin` role row: a temporary admin, a scoped admin and an admin
with a tier ceiling. The operator chose a narrower level (2026-10-22):

    dba = a PERMANENT (no end date) and UNSCOPED (all teams, all targets)
          `admin` role row. A tier ceiling is allowed. A super-admin passes
          too. A temporary admin and a scoped admin do not pass. An
          approver-only role does not pass.

These tests hold that definition at three heights: the rule over role rows
(`access.is_dba`), the gate (`require_admin(claims, "dba")`) and the five
routes, each called by every kind of caller. A guard test pins WHICH routes
take the level, so a fifth route needs a deliberate edit here. The Slack
`/sql kill` sets the same switch as `POST /admin/kill`, so it takes the same
level, and a guard test fails when one of the two doors changes alone.

The callers are role rows, the shape `access.roles` returns. The gate runs its
real code over them, with `access_model_v2` on, as in production.
"""
import ast
import itertools
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

from queryhub import access, admins, db, requesters, teams
from queryhub.slack_app import subcommands
from queryhub.web import admin as web_admin
from queryhub.web import app as web_app
from queryhub.web import routes_admin as ra
from queryhub.web import routes_data, sessions

ROUTES_ADMIN = Path(ra.__file__)
SUBCOMMANDS = Path(subcommands.__file__)
_REAL_PROBE = ra._probe          # the `world` fixture stands in for it

# ---------------------------------------------------------------------------
# The callers
# ---------------------------------------------------------------------------

SUPER = "U0EXAMPLE001"           # unscoped, uncapped, permanent
CAPPED = "U0EXAMPLE002"          # unscoped, permanent, approves up to RO
TEMP = "U0EXAMPLE003"            # unscoped admin with an end date
TEAM_SCOPED = "U0EXAMPLE004"     # admin for one team
TARGET_SCOPED = "U0EXAMPLE005"   # admin for one target
APPROVER = "U0EXAMPLE006"        # a pod lead: approver on one target, RO
FLEET_APPROVER = "U0EXAMPLE007"  # approver everywhere, at every tier
REQUESTER = "U0EXAMPLE008"       # no role at all
SUBJECT = "U0EXAMPLE009"         # the person whose access a DBA reads

LATER = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _role(role="admin", **over) -> dict:
    """One `access.roles` row. The default is a super-admin's row."""
    row = {"role": role, "scope_team_id": None, "all_teams": True,
           "scope_target_id": None, "all_targets": True, "max_tier": None,
           "any_tier": True, "valid_until": None}
    row.update(over)
    return row


PERSONAS = {
    SUPER: [_role()],
    CAPPED: [_role(max_tier="ro", any_tier=False)],
    TEMP: [_role(valid_until=LATER)],
    TEAM_SCOPED: [_role(all_teams=False, scope_team_id=7)],
    TARGET_SCOPED: [_role(all_targets=False, scope_target_id=42)],
    APPROVER: [_role("approver", all_targets=False, scope_target_id=42,
                     max_tier="ro", any_tier=False)],
    FLEET_APPROVER: [_role("approver")],
    REQUESTER: [],
}

PASSES = {SUPER, CAPPED}
NEED_DBA = "Refused: you need DBA access."
NEED_ADMIN = "Refused: you need admin access."
REFUSAL = {TEMP: NEED_DBA, TEAM_SCOPED: NEED_DBA, TARGET_SCOPED: NEED_DBA,
           APPROVER: NEED_ADMIN, FLEET_APPROVER: NEED_ADMIN,
           REQUESTER: NEED_ADMIN}


@pytest.fixture
def personas(monkeypatch):
    monkeypatch.setattr(teams, "use_v2", lambda: True)
    monkeypatch.setattr(access, "roles",
                        lambda pid: [dict(r) for r in PERSONAS.get(pid, [])])


def test_every_caller_has_an_expected_answer():
    """Guard the guard: a caller without an expected answer would make the
    matrix below skip a case without a sound."""
    assert set(PERSONAS) == PASSES | set(REFUSAL)
    assert not PASSES & set(REFUSAL)


# ---------------------------------------------------------------------------
# The rule over role rows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("uid", list(PERSONAS))
def test_the_rule_over_one_callers_rows(uid):
    assert access.is_dba(uid, rows=PERSONAS[uid]) is (uid in PASSES)


def test_the_rule_over_every_combination_of_the_five_conditions():
    """All 32 rows: the answer is yes only for a permanent `admin` row with
    both wildcards. The tier wildcard does not matter, which is the one
    difference from a super-admin."""
    for role, teams_all, targets_all, any_tier, permanent in itertools.product(
            ("admin", "approver"), (True, False), (True, False), (True, False),
            (True, False)):
        row = _role(role, all_teams=teams_all, all_targets=targets_all,
                    any_tier=any_tier, max_tier=None if any_tier else "ro",
                    scope_team_id=None if teams_all else 7,
                    scope_target_id=None if targets_all else 42,
                    valid_until=None if permanent else LATER)
        want = role == "admin" and teams_all and targets_all and permanent
        assert access.is_dba("x", rows=[row]) is want, row
        # A super-admin is a DBA. A DBA need not be a super-admin.
        if access.is_super_admin("x", rows=[row]):
            assert access.is_dba("x", rows=[row])


def test_one_qualifying_row_is_enough():
    """A person can hold a temporary row and a permanent one. The permanent
    one decides, as for every other role question."""
    assert access.is_dba("x", rows=[_role(valid_until=LATER),
                                    _role(max_tier="rw", any_tier=False)])
    assert not access.is_dba("x", rows=[])


def test_the_wrapper_follows_the_model_switch(monkeypatch):
    """`admins.is_dba` asks the new model when the switch is on and the
    `admins` table when it is off. The SQL itself runs against a real database
    in tests/test_integration_dba_level.py."""
    monkeypatch.setattr(teams, "use_v2", lambda: True)
    monkeypatch.setattr(access, "is_dba", lambda pid: pid == CAPPED)
    assert admins.is_dba(CAPPED) and not admins.is_dba(TEMP)

    seen = []
    monkeypatch.setattr(teams, "use_v2", lambda: False)
    monkeypatch.setattr(admins.db, "fetch_one",
                        lambda sql, params=None: seen.append(" ".join(sql.split())) or {"x": 1})
    assert admins.is_dba(CAPPED)
    [sql] = seen
    assert "FROM admins" in sql and "enabled = TRUE" in sql
    assert "scope_team_ids IS NULL" in sql and "scope_target_ids IS NULL" in sql
    # A tier ceiling is allowed, and a temporary grant never counts.
    assert "max_tier" not in sql and "temp_admin" not in sql


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("uid", list(PERSONAS))
def test_the_dba_gate(personas, uid):
    claims = {"sub": uid}
    if uid in PASSES:
        assert web_admin.require_admin(claims, "dba") == uid
        return
    with pytest.raises(HTTPException) as e:
        web_admin.require_admin(claims, "dba")
    assert e.value.status_code == 403
    assert e.value.detail == {"code": "forbidden", "message": REFUSAL[uid]}


ADMINS = {SUPER, CAPPED, TEMP, TEAM_SCOPED, TARGET_SCOPED}


@pytest.mark.parametrize("uid", list(PERSONAS))
def test_review_and_access_did_not_move(personas, uid):
    """`review` passes every admin and `access` passes a super-admin only,
    as before the new level."""
    claims = {"sub": uid}
    if uid in ADMINS:
        assert web_admin.require_admin(claims, "review") == uid
    else:
        with pytest.raises(HTTPException) as e:
            web_admin.require_admin(claims, "review")
        assert e.value.detail["message"] == NEED_ADMIN
    if uid == SUPER:
        assert web_admin.require_admin(claims, "access") == uid
        return
    with pytest.raises(HTTPException) as e:
        web_admin.require_admin(claims, "access")
    assert e.value.detail["message"] == (
        "Refused: you need super-admin access." if uid in ADMINS else NEED_ADMIN)


@pytest.mark.parametrize("level", ["DBA", "super", "", "dba "])
def test_an_unknown_level_fails_closed(personas, level):
    """Before the new level, any value other than "access" meant "review". A
    typo in a level name must not open a route to every admin."""
    with pytest.raises(ValueError):
        web_admin.require_admin({"sub": SUPER}, level)


def test_the_levels_are_the_four_known_ones():
    """`approve` is the approval routes' level (tests/test_approver_queue_gate.py)."""
    assert web_admin.LEVELS == ("review", "approve", "dba", "access")


# ---------------------------------------------------------------------------
# The five routes, called by every caller
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None


class FakeTxn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


def _target_rows() -> list[dict]:
    """Two registry rows in `targets.list_admin_rows` shape, with every key
    filled, so that a key the allowlist drops is visible when it leaks."""
    creds = {"ro": {"username": "reader_ro", "configured": True, "placeholder": False},
             "rw": {"username": "writer_rw", "configured": True, "placeholder": False},
             "ddl": {"username": None, "configured": False, "placeholder": False}}
    return [
        {"id": 42, "alias": "prod-main", "host": "db.example.internal",
         "port": 5432, "default_database": "ledger", "enabled": True,
         "notes": "Reached through a tunnel. The password is in the vault.",
         "engine": "postgres", "secrets_provider": "local",
         "tags": {"provider": "aws", "service": "rds", "account": "example-account"},
         "replica_of": None, "replica_of_alias": None, "deleted_at": None,
         "super_admin_only": False, "deleted_reason": None, "credentials": creds},
        {"id": 43, "alias": "prod-main-replica", "host": "replica.example.internal",
         "port": 5433, "default_database": "ledger", "enabled": False,
         "notes": None, "engine": "postgres", "secrets_provider": "awssm",
         "tags": {}, "replica_of": 42, "replica_of_alias": "prod-main",
         "deleted_at": datetime(2026, 7, 14, 9, 30, tzinfo=timezone.utc),
         "super_admin_only": True,
         "deleted_reason": "inventory reports the instance deleted",
         "credentials": {k: dict(v) for k, v in creds.items()}},
    ]


OWNERS = [{"target_id": 42, "id": 7, "name": "payments-be",
           "display_name": "Payments BE", "source": "pod-sync",
           "leads": ["Ada Lead"]}]


@pytest.fixture
def world(personas, monkeypatch):
    """Every read and write the five route bodies make, stood in for."""
    st = {"audit": [], "cursors": [], "probes": [],
          "probe": {"ok": True, "latencyMs": 4, "serverVersion": "17.2", "error": None}}

    def _txn():
        cur = FakeCursor()
        st["cursors"].append(cur)
        return FakeTxn(cur)

    def _probe(*args, **kw):
        st["probes"].append(args)
        return dict(st["probe"])

    monkeypatch.setattr(ra.db, "transaction", _txn)
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        st["audit"].append((uid, action, details)))
    monkeypatch.setattr(ra.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(ra.db, "fetch_all",
                        lambda sql, params=None: OWNERS if "target_team" in sql else [])
    monkeypatch.setattr(ra, "_effective_access_v2", lambda sid: ([], [], None))
    monkeypatch.setattr(ra, "_blocked_v2", lambda sid, teams_of: [])
    monkeypatch.setattr(ra.targets, "list_admin_rows", _target_rows)
    monkeypatch.setattr(routes_data, "_catalog_databases_map",
                        lambda ids: {42: ["ledger", "audit"]})
    monkeypatch.setattr(ra.targets, "by_alias",
                        lambda alias: SimpleNamespace(id=42) if alias == "prod-main" else None)
    monkeypatch.setattr(ra.targets, "admin_row", lambda tid: _target_rows()[0])
    monkeypatch.setattr(ra.targets, "get_credentials",
                        lambda tid, mode: ("reader_ro", "s3cret-ro-pass"))
    monkeypatch.setattr(ra, "_probe", _probe)
    return st


ROUTES = {
    "POST /api/admin/kill":
        lambda claims: ra.set_kill(ra.KillIn(enabled=False), claims=claims),
    "GET /api/admin/people/{slack_id}/effective-access":
        lambda claims: ra.admin_effective_access(SUBJECT, claims=claims),
    "GET /api/admin/connections":
        lambda claims: ra.admin_connections(claims=claims),
    "POST /api/admin/connections/{conn}/test":
        lambda claims: ra.admin_test_connection("prod-main", claims=claims),
    "GET /api/admin/people":
        lambda claims: ra.admin_people(claims=claims),
}


@pytest.mark.parametrize("route", list(ROUTES))
@pytest.mark.parametrize("uid", list(PERSONAS))
def test_each_route_takes_the_dba_level(world, route, uid):
    claims = {"sub": uid, "name": "Jordan Ray"}
    if uid in PASSES:
        assert isinstance(ROUTES[route](claims), dict)
        return
    with pytest.raises(HTTPException) as e:
        ROUTES[route](claims)
    assert e.value.status_code == 403
    assert e.value.detail == {"code": "forbidden", "message": REFUSAL[uid]}
    # Refused before the body ran: no write, no audit row, no probe.
    assert world["cursors"] == [] and world["audit"] == [] and world["probes"] == []


@pytest.mark.parametrize("enabled", [True, False])
def test_a_dba_stops_and_resumes_the_fleet_and_it_is_audited(world, monkeypatch,
                                                             enabled):
    """U4a done: a DBA who is not a super-admin sets the switch both ways,
    and each change writes an audit row that names them."""
    # The sweep of running reads (PLA-1592 U4c) runs after the switch is
    # committed, and it writes its own audit row. This test checks the level
    # and the audit row of the switch. tests/test_kill_switch_cancels_reads.py
    # checks the sweep, so a stub stands in for it here.
    monkeypatch.setattr(ra.kill_switch, "stop_running_reads", lambda *a, **k: 0)
    out = ra.set_kill(ra.KillIn(enabled=enabled), claims={"sub": CAPPED})
    assert out["enabled"] is enabled
    [cur] = world["cursors"]
    [(sql, params)] = [c for c in cur.calls if "'kill_switch'" in c[0]]
    assert params == ("on" if enabled else "off",)
    [row] = [a for a in world["audit"] if a[1] == "kill_switch_set"]
    assert row[0] == CAPPED and row[2]["enabled"] is enabled


@pytest.fixture
def slack(personas, monkeypatch):
    """`/sql kill` through the Slack dispatcher, with every read and write
    stood in for. `run(uid, text)` returns what the caller reads."""
    st = {"audit": [], "cursors": [], "said": []}

    def _fetch_one(sql, params=None):
        if "FROM bot_config" in sql:
            return {"value": "on"}
        if "FROM admins" in sql:
            return {"name": "Jordan Ray"}
        return None

    def _txn():
        cur = FakeCursor()
        st["cursors"].append(cur)
        return FakeTxn(cur)

    monkeypatch.setattr(subcommands.db, "fetch_one", _fetch_one)
    monkeypatch.setattr(subcommands.db, "transaction", _txn)
    monkeypatch.setattr(subcommands.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        st["audit"].append((uid, action, details)))
    monkeypatch.setattr(subcommands.cfg, "invalidate_cache", lambda: None)

    def run(uid, text):
        subcommands.dispatch(text, uid, None, lambda msg: st["said"].append(msg["text"]))
        [said] = st["said"]
        return said

    st["run"] = run
    return st


@pytest.mark.parametrize("uid", list(PERSONAS))
def test_the_slack_door_takes_the_dba_level(slack, uid):
    """The same callers, the same answers as on the web. "off" is the way
    through: it changes the switch and cancels nothing."""
    said = slack["run"](uid, "kill off")
    if uid in PASSES:
        assert "set `kill_switch` to *off*" in said
        assert slack["audit"] == [(uid, "kill_switch_set",
                                   {"enabled": False, "from": "on", "to": "off",
                                    "via": "slack"})]
        return
    # Refused before the body ran: no write and no audit row.
    assert slack["cursors"] == [] and slack["audit"] == []
    if uid in ADMINS:
        assert said == ":lock: Refused: you need DBA access to set the kill switch."
    else:
        assert said == ":no_entry: Refused: `/sql kill` is for admins only."


def test_a_dba_reads_effective_access_and_the_read_is_audited(world):
    """U13 done: a DBA reads it. The route writes only the audit row."""
    out = ra.admin_effective_access(SUBJECT, claims={"sub": CAPPED})
    assert out["slackId"] == SUBJECT
    assert world["audit"] == [(CAPPED, "effective_access_viewed",
                               {"subject": SUBJECT, "targets": 0})]
    [cur] = world["cursors"]
    assert cur.calls == []


# ---------------------------------------------------------------------------
# U14: the connection list. A super-admin's answer is the old one, byte for
# byte. A DBA gets the allowlisted keys.
# ---------------------------------------------------------------------------

@pytest.fixture
def client(world, monkeypatch):
    """The real app, with a session cookie that names the caller."""
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(db, "init_pool", lambda: None)
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "verify_access",
                        lambda t: {"sub": t, "sid": "sid-" + t, "provider": "slack"}
                        if t in PERSONAS else None)
    monkeypatch.setattr(sessions, "session_alive", lambda sid, principal=None: True)
    monkeypatch.setattr(requesters, "is_allowed", lambda uid: True)
    with TestClient(web_app.create_app()) as c:
        yield c


def _get_connections(client, uid):
    client.cookies.set("qh_session", uid)
    return client.get("/api/admin/connections")


# What GET /api/admin/connections returned to a super-admin for the two rows
# above, before the DBA level existed. Captured by running this test against
# the code of main at 782225f. Compare bytes, not parsed JSON: a change in key
# order or in a value's type is a change for the panel that reads it.
#
# Changed on purpose once since: PLA-1592 U7 added `health` and `inRotation`
# at the end of each row. The fixture stores no check result (`health` null),
# and replica routing is off, so the replica is not in rotation (false). A row
# that is not a replica has `inRotation` null.
GOLDEN_SUPER_BODY = (
    b'{"connections":[{"id":"prod-main","name":"prod-main","engine":"PostgreSQL",'
    b'"engineId":"postgres","env":"production","enabled":true,'
    b'"host":"db.example.internal","port":5432,"defaultDatabase":"ledger",'
    b'"notes":"Reached through a tunnel. The password is in the vault.",'
    b'"secretsProvider":"local","tags":{"provider":"aws","service":"rds",'
    b'"account":"example-account"},"replicaOf":null,"deleted":false,'
    b'"deletedAt":null,"deletedReason":null,"superAdminOnly":false,'
    b'"credentials":{"ro":{"username":"reader_ro","configured":true,'
    b'"placeholder":false},"rw":{"username":"writer_rw","configured":true,'
    b'"placeholder":false},"ddl":{"username":null,"configured":false,'
    b'"placeholder":false}},"databases":[{"id":"ledger","name":"ledger"},'
    b'{"id":"audit","name":"audit"}],"owners":[{"id":7,"name":"payments-be",'
    b'"displayName":"Payments BE","syncedFrom":"pod-sync",'
    b'"leads":["Ada Lead"]}],'
    b'"health":null,"inRotation":null},'
    b'{"id":"prod-main-replica","name":"prod-main-replica","engine":"PostgreSQL",'
    b'"engineId":"postgres","env":"production","enabled":false,'
    b'"host":"replica.example.internal","port":5433,"defaultDatabase":"ledger",'
    b'"notes":null,"secretsProvider":"awssm","tags":{},"replicaOf":"prod-main",'
    b'"deleted":true,"deletedAt":"2026-07-14T09:30:00+00:00",'
    b'"deletedReason":"inventory reports the instance deleted",'
    b'"superAdminOnly":true,"credentials":{"ro":{"username":"reader_ro",'
    b'"configured":true,"placeholder":false},"rw":{"username":"writer_rw",'
    b'"configured":true,"placeholder":false},"ddl":{"username":null,'
    b'"configured":false,"placeholder":false}},"databases":[],"owners":[],'
    b'"health":null,"inRotation":false}]}'
)


def test_a_super_admin_gets_the_connection_list_byte_for_byte(client):
    r = _get_connections(client, SUPER)
    assert r.status_code == 200, r.text
    assert r.content == GOLDEN_SUPER_BODY


def test_a_dba_gets_each_connection_through_the_allowlist(client):
    full = _get_connections(client, SUPER).json()["connections"]
    r = _get_connections(client, CAPPED)
    assert r.status_code == 200, r.text
    rows = r.json()["connections"]
    assert len(rows) == len(full)
    for got, whole in zip(rows, full):
        # The allowlisted keys, with the super-admin's values, in its order.
        assert list(got.items()) == [(k, v) for k, v in whole.items()
                                     if k in ra.DBA_CONNECTION_FIELDS]
        assert {"credentials", "secretsProvider", "notes"}.isdisjoint(got)
    # Nothing that names a credential, its source or a tunnel, anywhere.
    for text in ("reader_ro", "writer_rw", "awssm", "tunnel", "vault", "password"):
        assert text not in r.text


PEOPLE_ROWS = [
    {"slack_user_id": "U0EXAMPLE101", "name": "Jordan Ray", "enabled": True,
     "kind": "requester", "last_query_at": datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)},
    {"slack_user_id": "U0EXAMPLE102", "name": None, "enabled": False,
     "kind": "grant_only", "last_query_at": None},
]


def _people(world, monkeypatch, uid):
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: PEOPLE_ROWS)
    return ra.admin_people(claims={"sub": uid, "name": "Jordan Ray"})["people"]


def test_a_super_admin_gets_the_whole_people_row(world, monkeypatch):
    rows = _people(world, monkeypatch, SUPER)
    assert [set(r) for r in rows] == [
        {"id", "handle", "slackId", "name", "initials", "enabled", "kind", "lastQueryAt"}] * 2
    assert rows[0]["enabled"] is True and rows[0]["lastQueryAt"] is not None


def test_a_dba_gets_each_person_with_an_id_and_a_name_only(world, monkeypatch):
    full = _people(world, monkeypatch, SUPER)
    rows = _people(world, monkeypatch, CAPPED)
    assert len(rows) == len(full) == 2
    for got, whole in zip(rows, full):
        # The allowlisted keys, with the super-admin's values, in its order.
        assert list(got.items()) == [(k, v) for k, v in whole.items()
                                     if k in ra.DBA_PEOPLE_FIELDS]
        assert set(got) == {"id", "name"}
    # A person with no name still has one to pick: the id, as for a super-admin.
    assert rows[1] == {"id": "U0EXAMPLE102", "name": "U0EXAMPLE102"}


def test_over_http_a_dba_reads_the_people_list_and_a_temporary_admin_does_not(client, monkeypatch):
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: PEOPLE_ROWS)
    client.cookies.set("qh_session", CAPPED)
    r = client.get("/api/admin/people")
    assert r.status_code == 200, r.text
    assert r.json() == {"people": [{"id": "U0EXAMPLE101", "name": "Jordan Ray"},
                                   {"id": "U0EXAMPLE102", "name": "U0EXAMPLE102"}]}
    for text in ("enabled", "kind", "lastQueryAt", "handle", "slackId", "initials"):
        assert text not in r.text
    client.cookies.set("qh_session", TEMP)
    r = client.get("/api/admin/people")
    assert r.status_code == 403
    assert r.json()["error"] == {"code": "forbidden", "message": NEED_DBA}


def test_the_people_allowlist_is_id_and_name_and_nothing_else():
    """Widening it is a decision: make it here, on purpose."""
    assert ra.DBA_PEOPLE_FIELDS == frozenset({"id", "name"})


def test_the_allowlist_names_only_keys_a_row_has(client):
    """A typo in the allowlist hides a field from a DBA without a sound."""
    full = _get_connections(client, SUPER).json()["connections"][0]
    assert ra.DBA_CONNECTION_FIELDS <= set(full), \
        sorted(ra.DBA_CONNECTION_FIELDS - set(full))


@pytest.mark.parametrize("key", sorted(ra.DBA_CONNECTION_FIELDS))
def test_no_allowlisted_key_names_a_secret(key):
    """The allowlist is the one place where a DBA's view widens. A key that
    names a credential, a secret, its source, a tunnel or free text does not
    belong in it."""
    for word in ("password", "passwd", "credential", "secret", "user", "token",
                 "key", "tunnel", "note", "vault", "dsn"):
        assert word not in key.lower(), f"{key} looks like it carries a {word}"


def test_a_refused_caller_gets_the_dba_message_over_http(client):
    """The API error envelope, for a temporary admin."""
    r = _get_connections(client, TEMP)
    assert r.status_code == 403
    assert r.json()["error"] == {"code": "forbidden", "message": NEED_DBA}


# ---------------------------------------------------------------------------
# U7: the stored health check and the rotation on the connection list
# ---------------------------------------------------------------------------

CHECKED = datetime(2026, 10, 22, 12, 5, tzinfo=timezone.utc)


def _stored_health(error):
    return {"target_server_id": 42, "ok": error is None, "checked_at": CHECKED,
            "failing_since": None if error is None else CHECKED, "error": error}


def test_a_dba_row_carries_health_and_rotation(world, monkeypatch):
    monkeypatch.setattr(ra.connection_health, "by_target",
                        lambda rows: {42: _stored_health("Connection refused.")})
    monkeypatch.setattr(ra.replicas, "in_rotation", lambda rows: {43: True})
    dba = ra.admin_connections(claims={"sub": CAPPED})["connections"]
    sup = ra.admin_connections(claims={"sub": SUPER})["connections"]
    assert dba[0]["health"] == {"ok": False, "checkedAt": CHECKED.isoformat(),
                                "failingSince": CHECKED.isoformat(),
                                "error": "Connection refused."}
    assert (dba[0]["inRotation"], dba[1]["inRotation"]) == (None, True)
    assert dba[1]["health"] is None
    # A text with no credential in it reads the same for both callers.
    assert [r["health"] for r in dba] == [r["health"] for r in sup]


def test_a_dba_never_reads_a_stored_user_name_in_health(world, monkeypatch):
    """The check hid the login that it used. The other stored names, and a
    name changed after the check, are hidden again for a DBA. A replica has no
    login of its own, so its primary's names count for it."""
    rows = _target_rows()
    rows[1]["credentials"] = {k: {"username": None, "configured": False,
                                  "placeholder": False} for k in ("ro", "rw", "ddl")}
    monkeypatch.setattr(ra.targets, "list_admin_rows", lambda: rows)
    stored = {42: _stored_health("Login failed for user 'reader_ro'. "
                                 "The writer_rw login is locked."),
              43: {**_stored_health("Code: 516. DB::Exception: reader_ro: "
                                    "Authentication failed."), "target_server_id": 43}}
    monkeypatch.setattr(ra.connection_health, "by_target", lambda rows: stored)
    dba = ra.admin_connections(claims={"sub": CAPPED})["connections"]
    assert dba[0]["health"]["error"] == (
        "Login failed for user '[redacted]'. The [redacted] login is locked.")
    assert dba[1]["health"]["error"] == (
        "Code: 516. DB::Exception: [redacted]: Authentication failed.")
    assert "reader_ro" not in json.dumps(dba) and "writer_rw" not in json.dumps(dba)
    # A super-admin reads the stored text: the row shows the names anyway.
    sup = ra.admin_connections(claims={"sub": SUPER})["connections"]
    assert "reader_ro" in sup[0]["health"]["error"]


# ---------------------------------------------------------------------------
# U14: the test of a stored credential
# ---------------------------------------------------------------------------

# How four drivers name the user when a login fails. `errors.scrub` leaves
# the name in each of them (measured 2026-10-22).
NAMED_USER_ERRORS = [
    "Login failed for user 'reader_ro'.",                       # SQL Server
    "Code: 516. DB::Exception: reader_ro: Authentication failed: password is "
    "incorrect, or there is no user with such name.",          # ClickHouse
    'Role "reader_ro" does not exist',                          # PostgreSQL
    'Pg_hba.conf rejects connection for host "the host", user "reader_ro", '
    'database "ledger", SSL encryption',                        # PostgreSQL
]


@pytest.mark.parametrize("error", NAMED_USER_ERRORS)
def test_a_dba_never_sees_the_stored_user_name(world, error):
    world["probe"] = {"ok": False, "latencyMs": 9, "serverVersion": None,
                      "error": error}
    out = ra.admin_test_connection("prod-main", claims={"sub": CAPPED})
    assert out["ok"] is False and out["latencyMs"] == 9
    assert "reader_ro" not in out["error"]
    assert "[redacted]" in out["error"]
    assert set(out) == {"ok", "latencyMs", "serverVersion", "error"}


def test_a_dba_never_sees_the_password_either(world):
    """No driver here echoes a password. If one does, it stays hidden."""
    world["probe"] = {"ok": False, "latencyMs": 9, "serverVersion": None,
                      "error": "Authentication failed: s3cret-ro-pass is not valid"}
    out = ra.admin_test_connection("prod-main", claims={"sub": CAPPED})
    assert "s3cret-ro-pass" not in out["error"]
    assert out["error"] == "Authentication failed: [redacted] is not valid"


def test_the_name_goes_only_where_it_is_a_whole_word():
    """A short user name must not cut other words in the message."""
    answer = {"ok": False, "latencyMs": 9, "serverVersion": None,
              "error": "Login failed for user 'sa'. Reason: usage."}
    out = ra._dba_test_answer(answer, "sa", "pw-not-in-text")
    assert out["error"] == "Login failed for user '[redacted]'. Reason: usage."
    assert answer["error"].endswith("usage."), "the probe's own answer changed"


def test_a_super_admin_gets_the_probe_answer_unchanged(world):
    world["probe"] = {"ok": False, "latencyMs": 9, "serverVersion": None,
                      "error": NAMED_USER_ERRORS[0]}
    out = ra.admin_test_connection("prod-main", claims={"sub": SUPER})
    assert out == world["probe"]


def test_a_dba_success_is_the_probe_answer(world):
    out = ra.admin_test_connection("prod-main", claims={"sub": CAPPED})
    assert out == {"ok": True, "latencyMs": 4, "serverVersion": "17.2", "error": None}
    assert world["probes"] == [("postgres", "db.example.internal", 5432, "ledger",
                                "reader_ro", "s3cret-ro-pass")]


def test_the_real_probe_hides_the_user_from_a_dba(world, monkeypatch):
    """End to end through `_probe` itself, on the SQL Server path."""
    monkeypatch.setattr(ra, "_probe", _REAL_PROBE)
    from queryhub import mssql_exec

    def _refuse(*a, **k):
        raise RuntimeError("('28000', \"[28000] [Microsoft][ODBC Driver 18 for SQL "
                           "Server][SQL Server]Login failed for user 'reader_ro'. "
                           "(18456) (SQLDriverConnect)\")")
    monkeypatch.setattr(mssql_exec, "connect", _refuse)
    rows = _target_rows()
    rows[0]["engine"] = "mssql"
    monkeypatch.setattr(ra.targets, "admin_row", lambda tid: rows[0])
    dba = ra.admin_test_connection("prod-main", claims={"sub": CAPPED})
    assert dba["ok"] is False
    assert "reader_ro" not in dba["error"]
    assert "Login failed" in dba["error"]
    # The super-admin's answer is what `_probe` says, as before.
    sup = ra.admin_test_connection("prod-main", claims={"sub": SUPER})
    assert "reader_ro" in sup["error"]


# ---------------------------------------------------------------------------
# Which routes take the level
# ---------------------------------------------------------------------------

DBA_ROUTES = {
    "set_kill": ("POST", "/api/admin/kill"),
    "admin_effective_access": ("GET", "/api/admin/people/{slack_id}/effective-access"),
    "admin_connections": ("GET", "/api/admin/connections"),
    "admin_test_connection": ("POST", "/api/admin/connections/{conn}/test"),
    "admin_people": ("GET", "/api/admin/people"),
}

# Routes that the request named and that stay where they are.
STAY_SUPER_ADMIN = ("admin_test_new_connection", "admin_create_connection",
                    "admin_update_connection", "admin_delete_connection",
                    "admin_bulk_update_connections", "admin_config",
                    "admin_save_config", "admin_team_effective_access")


def _levels() -> dict[str, list]:
    """{function name: [level of each require_admin call in it]}, parsed."""
    out: dict[str, list] = {}
    for fn in ast.walk(ast.parse(ROUTES_ADMIN.read_text(encoding="utf-8"))):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(fn):
            if not (isinstance(call, ast.Call)
                    and getattr(call.func, "attr", None) == "require_admin"):
                continue
            level = "review"                       # the default
            if len(call.args) > 1:
                level = getattr(call.args[1], "value", call.args[1])
            for kw in call.keywords:
                if kw.arg == "need":
                    level = getattr(kw.value, "value", kw.value)
            out.setdefault(fn.name, []).append(level)
    return out


def test_exactly_five_routes_take_the_dba_level():
    """A fifth route on the DBA level is a decision. Make it here, on purpose."""
    found = {name for name, levels in _levels().items() if "dba" in levels}
    assert found == set(DBA_ROUTES)
    paths = {(m, route.path) for route in ra.router.routes
             if getattr(route, "endpoint", None) is not None
             and route.endpoint.__name__ in DBA_ROUTES
             for m in route.methods - {"HEAD", "OPTIONS"}}
    assert paths == set(DBA_ROUTES.values())


def test_every_level_in_the_admin_routes_is_a_known_literal():
    """A level held in a variable would hide a route from the check above."""
    for name, levels in _levels().items():
        for level in levels:
            assert level in web_admin.LEVELS, f"{name}: require_admin level {level!r}"


@pytest.mark.parametrize("name", STAY_SUPER_ADMIN)
def test_the_routes_that_stay_super_admin_only(name):
    assert _levels()[name] == ["access"], name


def test_reading_the_kill_switch_stays_open_to_every_admin():
    assert _levels()["get_kill"] == ["review"]


# The check in `admins` that answers each level of `require_admin`.
LEVEL_CHECK = {"review": "is_admin", "approve": "holds_approval_role",
               "dba": "is_dba", "access": "is_super_admin"}


def _slack_kill_checks() -> set[str]:
    """The `admins.<check>(...)` calls in the Slack `/sql kill`, parsed."""
    tree = ast.parse(SUBCOMMANDS.read_text(encoding="utf-8"))
    [fn] = [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_handle_kill"]
    return {call.func.attr for call in ast.walk(fn)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name) and call.func.value.id == "admins"}


def test_the_two_kill_switch_doors_take_the_same_level():
    """`POST /admin/kill` and `/sql kill` set the same switch. When one door
    changes its level, this fails until the other door follows."""
    [web] = _levels()["set_kill"]
    assert _slack_kill_checks() == {LEVEL_CHECK[web]}


def test_the_door_check_sees_a_change():
    """Guard the guard: the parse finds the check that is in the source."""
    assert _slack_kill_checks() == {"is_dba"}
    assert set(LEVEL_CHECK) == set(web_admin.LEVELS)


def test_the_golden_body_is_valid_json():
    """The golden bytes above are one JSON document, so a bad edit to them
    fails here with a clear reason and not as a byte mismatch."""
    assert json.loads(GOLDEN_SUPER_BODY)["connections"][0]["id"] == "prod-main"
