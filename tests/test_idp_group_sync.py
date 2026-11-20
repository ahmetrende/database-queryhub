"""Team membership from company-SSO groups (idp_group_sync.py, migration 222).

The sync adds and removes only `source = 'idp'` rows. A manual membership is
never touched. It is off by default, skips Slack and a missing `groups` claim,
and never blocks a login. Every change commits with its audit row.
"""
from __future__ import annotations

import copy
import inspect
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from queryhub import idp_group_sync as sync
from queryhub.web import auth_providers, routes_admin as ra, routes_auth


# ---- a small in-memory model of the tables the sync reads and writes -------

class _DB:
    def __init__(self, v2: bool = True):
        self.v2 = v2
        self.settings = {"idp_group_sync_enabled": "on"}
        self.teams = {1, 2, 3}
        self.mappings: list[tuple[int, str, str]] = []
        self.members: list[dict] = []
        self.known = True
        self.log: list[tuple] = []
        self.txn = 0
        self.fail_on: str | None = None
        self._next_id = 100

    def member(self, team_id, source="manual", idp_group=None):
        self._next_id += 1
        self.members.append({"id": self._next_id, "team_id": team_id,
                             "source": source, "idp_group": idp_group,
                             "deleted": False})

    def live(self):
        return [m for m in self.members if not m["deleted"]]


class _Cur:
    def __init__(self, d: _DB):
        self.d = d
        self.rows: list[dict] = []

    def execute(self, sql, params=()):
        d = self.d
        d.log.append(("sql", sql, params, self))
        if d.fail_on and d.fail_on in sql:
            raise RuntimeError("boom")
        s = " ".join(sql.split())
        if "FROM team_idp_groups g" in s:
            self.rows = [{"team_id": t, "group_name": g} for t, p, g in d.mappings
                         if p == params[0] and t in d.teams]
        elif "FROM team_idp_groups" in s and "provider <>" in s:
            tids, prov = params
            self.rows = [{"team_id": t, "provider": p, "group_name": g}
                         for t, p, g in d.mappings if t in tids and p != prov]
        elif "FROM principal_identity" in s:
            self.rows = [{"principal_id": 900}] if d.known else []
        elif s.startswith("SELECT") and ("FROM team_member " in s
                                         or "FROM team_members " in s):
            self.rows = [dict(m) for m in d.live()]
        elif s.startswith("UPDATE team_member "):
            hit = [m for m in d.live() if m["id"] == params[0] and m["source"] == "idp"]
            for m in hit:
                m["deleted"] = True
            self.rows = [{"id": m["id"]} for m in hit]
        elif s.startswith("DELETE FROM team_members "):
            hit = [m for m in d.live() if m["team_id"] == params[0] and m["source"] == "idp"]
            for m in hit:
                m["deleted"] = True
            self.rows = [{"team_id": m["team_id"]} for m in hit]
        elif s.startswith("INSERT INTO team_member (") or s.startswith("INSERT INTO team_members ("):
            tid, _key, group = params
            if any(m["team_id"] == tid for m in d.live()):
                self.rows = []
            else:
                d.member(tid, "idp", group)
                self.rows = [{"id": d._next_id, "team_id": tid}]
        else:
            self.rows = []

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


@pytest.fixture
def d(monkeypatch):
    state = _DB()
    cur = _Cur(state)

    @contextmanager
    def txn():
        state.txn += 1
        snapshot = copy.deepcopy(state.members)
        try:
            yield cur
        except Exception:
            state.members = snapshot
            raise

    monkeypatch.setattr(sync.db, "transaction", txn)
    monkeypatch.setattr(sync.cfg, "get_setting",
                        lambda key, default=None: state.settings.get(key, default))
    monkeypatch.setattr(sync.teams_mod, "use_v2", lambda: state.v2)
    monkeypatch.setattr(auth_providers, "oidc_ids", lambda: ["corp", "other"])
    monkeypatch.setattr(sync.audit, "log_in",
                        lambda c, rid, actor, name, action, details:
                        state.log.append(("audit", action, details, c)))
    state.cur = cur
    return state


def _writes(d):
    return [e for e in d.log if e[0] == "audit"
            or (e[0] == "sql" and not e[1].lstrip().startswith("SELECT"))]


# ---- skips -----------------------------------------------------------------

def test_flag_off_skips_without_a_transaction(d):
    d.settings["idp_group_sync_enabled"] = "off"
    d.mappings = [(1, "corp", "dba")]
    out = sync.sync_on_login("U1", "corp", ["dba"])
    assert out == {"added": [], "removed": [], "skipped": "disabled"}
    assert d.txn == 0 and d.log == []


def test_the_flag_defaults_to_off(d):
    d.settings.clear()
    assert sync.sync_on_login("U1", "corp", ["dba"])["skipped"] == "disabled"


@pytest.mark.parametrize("provider", ["slack", "local", "notconfigured", ""])
def test_slack_and_non_oidc_providers_skip(d, provider):
    out = sync.sync_on_login("U1", provider, ["dba"])
    assert out["skipped"] == "not_oidc"
    assert d.txn == 0


def test_a_missing_groups_claim_skips(d):
    d.member(1, "idp", "dba")
    out = sync.sync_on_login("U1", "corp", None)
    assert out["skipped"] == "no_groups_claim"
    assert d.txn == 0 and d.live()[0]["source"] == "idp"


# ---- adds and removes --------------------------------------------------------

def test_an_empty_list_removes_idp_rows_only(d):
    d.mappings = [(1, "corp", "dba"), (2, "corp", "ops")]
    d.member(1, "idp", "dba")
    d.member(2, "manual")
    out = sync.sync_on_login("U1", "corp", [])
    assert out == {"added": [], "removed": [1], "skipped": None}
    assert [(m["team_id"], m["source"]) for m in d.live()] == [(2, "manual")]


def test_a_mapped_group_adds_an_idp_row(d):
    d.mappings = [(1, "corp", "DBA Team")]
    out = sync.sync_on_login("U1", "corp", ["DBA Team", "Unrelated"])
    assert out == {"added": [1], "removed": [], "skipped": None}
    (row,) = d.live()
    assert (row["team_id"], row["source"], row["idp_group"]) == (1, "idp", "DBA Team")


def test_group_match_is_exact(d):
    d.mappings = [(1, "corp", "DBA Team")]
    assert sync.sync_on_login("U1", "corp", ["dba team"])["added"] == []


def test_a_manual_row_is_not_duplicated_or_changed(d):
    d.mappings = [(1, "corp", "dba")]
    d.member(1, "manual")
    out = sync.sync_on_login("U1", "corp", ["dba"])
    assert out["added"] == [] and out["removed"] == []
    assert [(m["team_id"], m["source"], m["idp_group"]) for m in d.live()] == \
        [(1, "manual", None)]
    assert _writes(d) == []


def test_a_manual_row_is_never_removed(d):
    d.mappings = [(1, "corp", "dba")]
    d.member(1, "manual")
    d.member(2, "manual")
    out = sync.sync_on_login("U1", "corp", [])
    assert out["removed"] == []
    assert len(d.live()) == 2


def test_an_idp_row_leaves_with_its_group(d):
    d.mappings = [(1, "corp", "dba")]
    d.member(1, "idp", "dba")
    out = sync.sync_on_login("U1", "corp", ["something-else"])
    assert out["removed"] == [1]
    assert d.live() == []


def test_an_unmapped_group_is_ignored(d):
    out = sync.sync_on_login("U1", "corp", ["nobody-maps-this"])
    assert out == {"added": [], "removed": [], "skipped": None}
    assert _writes(d) == []


def test_a_deleted_mapping_removes_the_idp_row_at_next_login(d):
    d.member(1, "idp", "dba")           # mapping already deleted by an admin
    out = sync.sync_on_login("U1", "corp", ["dba"])
    assert out["removed"] == [1]


def test_a_row_another_provider_explains_is_left_alone(d):
    d.mappings = [(1, "other", "dba")]
    d.member(1, "idp", "dba")
    out = sync.sync_on_login("U1", "corp", [])
    assert out["removed"] == []
    assert len(d.live()) == 1


def test_an_unknown_principal_changes_nothing(d):
    d.known = False
    d.mappings = [(1, "corp", "dba")]
    out = sync.sync_on_login("U1", "corp", ["dba"])
    assert out == {"added": [], "removed": [], "skipped": None}
    assert _writes(d) == []


def test_a_mapping_to_a_deleted_team_does_nothing(d):
    d.teams = {2}
    d.mappings = [(1, "corp", "dba")]
    assert sync.sync_on_login("U1", "corp", ["dba"])["added"] == []


def test_legacy_model_writes_team_members(d):
    d.v2 = False
    d.mappings = [(1, "corp", "dba")]
    d.member(2, "idp", "gone")
    out = sync.sync_on_login("U1", "corp", ["dba"])
    assert out == {"added": [1], "removed": [2], "skipped": None}
    sqls = [e[1] for e in d.log if e[0] == "sql"]
    assert any(s.startswith("INSERT INTO team_members (") for s in sqls)
    assert any(s.startswith("DELETE FROM team_members") for s in sqls)
    assert not any("principal_identity" in s for s in sqls)


def test_v2_soft_deletes_and_uses_the_partial_index(d):
    src = inspect.getsource(sync._sync_in)
    assert "SET is_deleted = TRUE" in src
    assert "ON CONFLICT (team_id, principal_id) WHERE NOT is_deleted" in src
    assert "source = 'idp'" in src


# ---- atomic audit ------------------------------------------------------------

def test_every_change_writes_its_audit_row_in_the_same_transaction(d):
    d.mappings = [(1, "corp", "dba")]
    d.member(2, "idp", "ops")
    out = sync.sync_on_login("U1", "corp", ["dba"])
    assert out["added"] == [1] and out["removed"] == [2]
    assert d.txn == 1
    w = _writes(d)
    assert [e[0] for e in w] == ["sql", "audit", "sql", "audit"]
    assert w[0][1].startswith("UPDATE team_member ")
    assert w[1][1] == "team_member_idp_removed"
    assert w[1][2] == {"team_id": 2, "provider": "corp", "group": "ops",
                       "principal": "U1"}
    assert w[2][1].startswith("INSERT INTO team_member (")
    assert w[3][1] == "team_member_idp_added"
    assert w[3][2] == {"team_id": 1, "provider": "corp", "group": "dba",
                       "principal": "U1"}
    # Same cursor as the writes: the rows commit together.
    assert all(e[3] is d.cur for e in w)


def test_the_auth_event_dm_is_not_suppressed():
    assert "auth_dm_suppress" not in inspect.getsource(sync)


# ---- failure never blocks a login --------------------------------------------

def test_an_error_inside_returns_skipped_error_and_rolls_back(d):
    d.mappings = [(1, "corp", "dba")]
    d.member(2, "idp", "ops")
    d.fail_on = "INSERT INTO team_member"
    out = sync.sync_on_login("U1", "corp", ["dba"])
    assert out == {"added": [], "removed": [], "skipped": "error"}
    assert [(m["team_id"], m["source"]) for m in d.live()] == [(2, "idp")]


def test_a_config_error_returns_skipped_error(d, monkeypatch):
    def boom(key, default=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(sync.cfg, "get_setting", boom)
    assert sync.sync_on_login("U1", "corp", ["dba"])["skipped"] == "error"


def test_the_callback_syncs_after_the_gates_and_before_the_session():
    src = inspect.getsource(routes_auth.auth_callback)
    i_sync = src.index("idp_group_sync.sync_on_login(")
    assert src.index("employment_verdict") < i_sync
    assert src.index("own_ui_allowed") < i_sync
    assert i_sync < src.index("sessions.create_session(")
    assert "ident.groups" in src[i_sync:i_sync + 200]


# ---- the groups claim ----------------------------------------------------------

def test_groups_from_claims():
    assert sync.groups_from_claims({}) is None
    assert sync.groups_from_claims({"groups": None}) is None
    assert sync.groups_from_claims({"groups": "dba"}) is None
    assert sync.groups_from_claims({"groups": []}) == []
    assert sync.groups_from_claims({"groups": ["DBA", 3, "", "Ops "]}) == ["DBA", "Ops "]


def test_the_oidc_identity_carries_the_groups(monkeypatch):
    from queryhub import requesters
    monkeypatch.setattr(auth_providers.cfg, "get_setting",
                        lambda key, default=None: default)
    monkeypatch.setattr(requesters, "principal_by_email",
                        lambda email: {"slack_user_id": "U1", "name": "Sam"})
    p = auth_providers.OIDCProvider("corp", "OIDC_CORP")
    with_groups = p._identity_from_claims(
        {"email": "sam@example.com", "groups": ["dba"]})
    assert with_groups.groups == ["dba"]
    without = p._identity_from_claims({"email": "sam@example.com"})
    assert without.groups is None


# ---- admin API ---------------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    state = {"gate": [], "audit": [], "sql": [], "txn": 0,
             "rows": [{"provider": "corp", "group_name": "dba",
                       "created_at": None, "created_by": "UADMIN"}],
             "team": {"id": 5, "name": "Payments", "source": None}}

    class Cur:
        def __init__(self):
            self._rows: list[dict] = []

        def execute(self, sql, params=()):
            state["sql"].append((sql, params))
            s = " ".join(sql.split())
            if s.startswith("SELECT"):
                self._rows = [dict(r) for r in state["rows"]]
            elif s.startswith("DELETE FROM team_idp_groups"):
                _tid, p, g = params
                state["rows"] = [r for r in state["rows"]
                                 if (r["provider"], r["group_name"]) != (p, g)]
            elif s.startswith("INSERT INTO team_idp_groups"):
                _tid, p, g, by = params
                state["rows"].append({"provider": p, "group_name": g,
                                      "created_at": None, "created_by": by})

        def fetchall(self):
            return self._rows

    cur = Cur()
    state["cur"] = cur

    @contextmanager
    def txn():
        state["txn"] += 1
        yield cur

    def require_admin(claims, scope):
        state["gate"].append(scope)
        return "UADMIN"

    monkeypatch.setattr(ra.admin, "require_admin", require_admin)
    monkeypatch.setattr(ra, "_team_for_write", lambda tid: state["team"])
    monkeypatch.setattr(ra.db, "transaction", txn)
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=(): state["rows"])
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda c, rid, u, n, a, det: state["audit"].append((a, det, c)))
    monkeypatch.setattr(auth_providers, "oidc_ids", lambda: ["corp"])
    monkeypatch.setattr(sync, "enabled", lambda: False)
    return state


CLAIMS = {"sub": "UADMIN", "name": "Admin"}


def _put(groups):
    body = ra.TeamIdpGroupsIn(groups=[ra.IdpGroupIn(provider=p, group=g)
                                      for p, g in groups])
    return ra.admin_set_team_idp_groups(5, CLAIMS, body)


def test_get_checks_the_gate_and_returns_the_list(api):
    out = ra.admin_get_team_idp_groups(5, CLAIMS)
    assert api["gate"] == ["access"]
    assert out == {"enabled": False,
                   "groups": [{"provider": "corp", "group": "dba",
                               "createdAt": ra.mapping.iso(None),
                               "createdBy": "UADMIN"}]}


def test_get_and_put_answer_404_for_an_unknown_team(api):
    api["team"] = None
    for call in (lambda: ra.admin_get_team_idp_groups(5, CLAIMS),
                 lambda: _put([("corp", "dba")])):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 404


def test_put_refuses_a_synced_team(api):
    api["team"] = {"id": 5, "name": "Pod", "source": "pod_sync"}
    with pytest.raises(HTTPException) as e:
        _put([("corp", "dba")])
    assert e.value.status_code == 409


@pytest.mark.parametrize("groups", [
    [("slack", "dba")],
    [("local", "dba")],
    [("Corp", "dba")],
    [("notconfigured", "dba")],
    [("corp", "")],
    [("corp", "   ")],
    [("corp", " dba")],
    [("corp", "x" * 201)],
    [("corp", f"g{i}") for i in range(51)],
])
def test_put_validates(api, groups):
    with pytest.raises(HTTPException) as e:
        _put(groups)
    assert e.value.status_code == 400
    assert api["txn"] == 0 and api["audit"] == []


def test_put_without_a_body_is_a_400(api):
    with pytest.raises(HTTPException) as e:
        ra.admin_set_team_idp_groups(5, CLAIMS, None)
    assert e.value.status_code == 400


def test_put_accepts_fifty_groups_and_200_chars(api):
    groups = [("corp", f"g{i}") for i in range(49)] + [("corp", "y" * 200)]
    out = _put(groups)
    assert len(out["groups"]) == 50


def test_put_replaces_the_list_with_one_audit_row(api):
    out = _put([("corp", "ops"), ("corp", "ops")])
    assert api["gate"] == ["access"]
    assert api["txn"] == 1
    (action, details, c) = api["audit"][0]
    assert len(api["audit"]) == 1
    assert action == "team_idp_groups_changed"
    assert details["team_id"] == 5
    assert details["before"] == [{"provider": "corp", "group": "dba"}]
    assert details["after"] == [{"provider": "corp", "group": "ops"}]
    assert c is api["cur"]
    assert [g["group"] for g in out["groups"]] == ["ops"]
    assert out["groups"][0]["createdBy"] == "UADMIN"


def test_put_with_no_change_writes_nothing(api):
    _put([("corp", "dba")])
    assert api["audit"] == []
    assert not any(s.lstrip().startswith(("INSERT", "DELETE"))
                   for s, _ in api["sql"])


def test_put_keeps_claims_before_the_body():
    params = list(inspect.signature(ra.admin_set_team_idp_groups).parameters)
    assert params.index("claims") < params.index("body")
