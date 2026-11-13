"""Adding a person: POST /api/admin/people.

No live database. A fake cursor records the SQL, and the reads go through a
small fake of `db.fetch_all` and `db.fetch_one`.
"""
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from queryhub.web import routes_admin as ra

NEW = "U0EXAMPLE001"
REF = "U0EXAMPLE002"


class _Cur:
    def __init__(self):
        self.sql = []
        self.params = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        self.sql.append(sql)
        self.params.append(params)

    def fetchall(self):
        return []

    def fetchone(self):
        # A new team asks for its id back.
        if self.sql and "RETURNING id" in self.sql[-1]:
            return {"id": 42}
        return None


@pytest.fixture
def wired(monkeypatch):
    state = {"gate": [], "audit": [], "copy_calls": [], "txn": 0}
    cur = _Cur()
    state["cur"] = cur

    @contextmanager
    def fake_txn():
        state["txn"] += 1
        yield cur

    teams = {3: "Team One", 4: "Team Two"}
    team_grants = [{"all_targets": False, "alias": "prod-main", "tier": "rw"}]

    def fetch_all(sql, params=()):
        if "FROM team" in sql and "access_grant" not in sql:
            return [{"id": i, "name": teams[i]} for i in params[0] if i in teams]
        if "FROM access_grant" in sql or "team_target_grants" in sql:
            return team_grants
        if "FROM target_servers" in sql:
            return [{"id": 7, "alias": "prod-main"}]
        return []

    def fetch_one(sql, params=()):
        if "FROM requesters" in sql:
            return {"name": "Sam New"}
        return None

    def require_admin(claims, scope):
        state["gate"].append(scope)
        return "UADMIN"

    state["resolve"] = {"principal": NEW, "known": False, "name": "Jordan Blake",
                        "email": "jordan.blake@example.com", "enabled": False,
                        "admin": False}

    def copy_core(c, slack_id, body, uid, actor_name):
        state["copy_calls"].append(body)
        if body.dryRun:
            raise ra._CopyPreview({"wouldWrite": ["prod-main"],
                                   "wouldJoinTeams": ["Team One"],
                                   "wouldCopyAutoApprove": 2})
        return {"writtenTargets": ["prod-main"], "teams": ["Team One"],
                "autoApproveCopied": 0}

    monkeypatch.setattr(ra.admin, "require_admin", require_admin)
    monkeypatch.setattr(ra.db, "transaction", fake_txn)
    monkeypatch.setattr(ra.db, "fetch_all", fetch_all)
    monkeypatch.setattr(ra.db, "fetch_one", fetch_one)
    monkeypatch.setattr(ra, "_resolve_principal", lambda pid: state["resolve"])
    monkeypatch.setattr(ra, "_slack_profile",
                        lambda pid: {"name": "Jordan Blake", "email": None, "tz": None})
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: True)
    monkeypatch.setattr(ra.teams_mod, "effective_grant_for_user",
                        lambda p, t: {"mode": "rw"})
    monkeypatch.setattr(ra.mapping, "env_of", lambda alias: "production")
    monkeypatch.setattr(ra, "_copy_access_core", copy_core)
    state["settings"] = {"web_allowed_email_domain": "example.com"}
    monkeypatch.setattr(ra.cfg, "get_setting",
                        lambda key, default=None: state["settings"].get(key, default))
    state["taken_teams"] = set()
    monkeypatch.setattr(
        ra, "_resolve_team",
        lambda name: {"id": 1, "name": name} if name in state["taken_teams"] else None)
    monkeypatch.setattr(ra, "_target_id_of",
                        lambda alias: {"prod-main": 7, "stage-main": 8, "queryhub": 9}.get(alias))
    monkeypatch.setattr(ra.grants, "control_plane_target_ids", lambda: {9})
    monkeypatch.setattr(ra.access_model, "target_is_super_admin_only", lambda tid: False)
    monkeypatch.setattr(ra.mapping, "env_of",
                        lambda alias: "production" if alias.startswith("prod") else "staging")
    state["grant_writes"] = []
    monkeypatch.setattr(ra.grants, "_write_grant",
                        lambda c, **kw: state["grant_writes"].append(kw))
    state["role_writes"] = []

    def write_role(c, uid, actor_name, body, tier):
        state["role_writes"].append((body, tier))
        return 55

    monkeypatch.setattr(ra, "_write_role", write_role)
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda c, r, u, n, a, d: state["audit"].append((a, d)))
    return state


def _add(**kw):
    kw.setdefault("principal", NEW)
    return ra.admin_add_person(ra.NewPersonIn(**kw), {"sub": "UADMIN", "name": "Admin"})


def test_the_gate_is_called(wired):
    _add()
    assert wired["gate"] == ["access"]


def test_a_bad_principal_is_refused(wired):
    with pytest.raises(HTTPException) as e:
        _add(principal="not an id")
    assert e.value.status_code == 400


def test_an_unknown_slack_id_is_refused(wired):
    wired["resolve"] = dict(wired["resolve"], name=None)
    with pytest.raises(HTTPException) as e:
        _add()
    assert e.value.status_code == 400


def test_a_known_person_is_a_conflict_on_apply(wired):
    wired["resolve"] = dict(wired["resolve"], known=True, enabled=True)
    with pytest.raises(HTTPException) as e:
        _add()
    assert e.value.status_code == 409
    assert wired["cur"].sql == []


def test_a_known_person_shows_known_on_a_dry_run(wired):
    wired["resolve"] = dict(wired["resolve"], known=True, enabled=True)
    assert _add(dryRun=True)["known"] is True


def _index(cur, text):
    return next(i for i, s in enumerate(cur.sql) if text in s)


def test_notify_false_suppresses_before_the_requester_insert(wired):
    _add(notify=False)
    cur = wired["cur"]
    assert _index(cur, "SET LOCAL") < _index(cur, "INSERT INTO requesters")


def test_notify_true_suppresses_after_the_requester_insert(wired):
    out = _add(notify=True)
    cur = wired["cur"]
    assert _index(cur, "INSERT INTO requesters") < _index(cur, "SET LOCAL")
    assert out["notified"] is True


def test_one_team_member_insert_per_team(wired):
    out = _add(teamIds=[3, 4])
    inserts = [s for s in wired["cur"].sql if "INSERT INTO team_member " in s]
    assert len(inserts) == 2
    assert out["teamsJoined"] == ["Team One", "Team Two"]


def test_an_unknown_team_is_refused(wired):
    with pytest.raises(HTTPException) as e:
        _add(teamIds=[99])
    assert e.value.status_code == 400


def test_copy_from_calls_the_core_and_keeps_auto_approve_off_by_default(wired):
    out = _add(copyFrom=REF)
    body = wired["copy_calls"][0]
    assert body.source == REF
    assert body.includeTeams is True
    assert body.includeAutoApprove is False
    assert out["copied"]["targets"] == ["prod-main"]


def test_copy_from_passes_auto_approve_through(wired):
    _add(copyFrom=REF, includeAutoApprove=True)
    assert wired["copy_calls"][0].includeAutoApprove is True


def test_copy_from_the_new_person_is_refused(wired):
    with pytest.raises(HTTPException) as e:
        _add(copyFrom=NEW)
    assert e.value.status_code == 400


def test_a_dry_run_writes_nothing_and_warns_on_rw_in_production(wired):
    out = _add(teamIds=[3], dryRun=True)
    assert wired["cur"].sql == []
    assert wired["audit"] == []
    assert out["dryRun"] is True
    assert out["empty"] is False
    assert out["teams"][0]["targets"][0]["tier"] == "RW"
    assert out["warnings"] == [
        {"connectionId": "prod-main", "tier": "RW", "source": "Team One"}]


def test_a_dry_run_copy_warns_with_the_reference_name(wired):
    out = _add(copyFrom=REF, dryRun=True)
    assert out["copy"]["fromName"] == "Sam New"
    assert out["copy"]["autoApprove"] == 2
    assert out["warnings"][0]["source"] == "Sam New"


def test_one_person_added_audit_row(wired):
    _add(teamIds=[3], copyFrom=REF)
    rows = [d for a, d in wired["audit"] if a == "person_added"]
    assert len(rows) == 1
    assert rows[0]["teams"] == ["Team One"]
    assert rows[0]["copy_from"] == REF
    assert len(wired["audit"]) == 1


def _g(**kw):
    kw.setdefault("connectionId", "prod-main")
    return ra.NewPersonGrantIn(**kw)


def test_grants_are_written_through_the_grant_writer_in_one_transaction(wired):
    out = _add(grants=[_g(databases=["orders"], tier="RW"),
                       _g(connectionId="stage-main", databases=["*"], tier="RO")])
    assert wired["txn"] == 1
    first, second = wired["grant_writes"]
    assert first["target_id"] == 7 and first["mode"] == "rw"
    assert first["databases"] == ["orders"] and first["grantee_id"] == NEW
    assert second["target_id"] == 8 and second["databases"] is None
    assert out["grantsWritten"] == ["prod-main", "stage-main"]


def test_the_control_plane_connection_is_refused(wired):
    with pytest.raises(HTTPException) as e:
        _add(grants=[_g(connectionId="queryhub")])
    assert e.value.status_code == 403
    assert wired["cur"].sql == [] and wired["grant_writes"] == []


def test_a_bad_grant_tier_or_connection_or_date_is_refused(wired):
    for bad, code in ((_g(tier="root"), 400), (_g(connectionId="nope"), 404),
                      (_g(expiresAt="2001-01-01T00:00:00Z"), 400),
                      (_g(expiresAt="soon"), 400)):
        with pytest.raises(HTTPException) as e:
            _add(grants=[bad])
        assert e.value.status_code == code
    assert wired["cur"].sql == []


def test_a_connection_listed_twice_is_refused(wired):
    with pytest.raises(HTTPException) as e:
        _add(grants=[_g(), _g(tier="RW")])
    assert e.value.status_code == 400


def test_a_dry_run_lists_grants_and_warns_on_rw_in_production(wired):
    out = _add(dryRun=True, grants=[_g(tier="DDL"), _g(connectionId="stage-main", tier="RW")])
    assert wired["cur"].sql == [] and wired["grant_writes"] == []
    assert [(g["connectionId"], g["tier"], g["env"]) for g in out["grants"]] == [
        ("prod-main", "DDL", "production"), ("stage-main", "RW", "staging")]
    assert out["warnings"] == [{"connectionId": "prod-main", "tier": "DDL", "source": "Servers"}]
    assert out["empty"] is False


def test_the_role_goes_through_the_role_spec_check(wired):
    out = _add(role=ra.NewPersonRoleIn(role="approver", maxTier="rw", scopeTeamId=3))
    body, tier = wired["role_writes"][0]
    assert (body.subject, body.role, body.scopeTeamId, body.maxTier) == (NEW, "approver", 3, "rw")
    assert tier == "rw" and out["role"] == 55


def test_a_role_the_spec_refuses_writes_nothing(wired):
    for spec in (ra.NewPersonRoleIn(role="approver", maxTier="DDL"),
                 ra.NewPersonRoleIn(role="admin", scopeTeamId=3),
                 ra.NewPersonRoleIn(role="chief")):
        with pytest.raises(HTTPException) as e:
            _add(role=spec)
        assert e.value.status_code == 400
    assert wired["cur"].sql == [] and wired["role_writes"] == []


def test_a_dry_run_warns_about_an_admin_role(wired):
    out = _add(dryRun=True, role=ra.NewPersonRoleIn(role="admin"))
    assert out["role"] == {"role": "admin", "scopeTeamId": None,
                           "scopeTargetId": None, "maxTier": None}
    assert [w for w in out["warnings"] if w.get("kind") == "role"]
    assert wired["role_writes"] == []


def test_a_new_team_is_created_with_the_person_as_lead(wired):
    out = _add(newTeams=[ra.NewPersonTeamIn(name="Team Nine", lead=True),
                         ra.NewPersonTeamIn(name="Team Ten")])
    cur = wired["cur"]
    members = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO team_member " in s]
    assert members == [(42, True, NEW), (42, False, NEW)]
    created = [d for a, d in wired["audit"] if a == "team_created"]
    assert [d["team"] for d in created] == ["Team Nine", "Team Ten"]
    assert out["teamsCreated"] == ["Team Nine", "Team Ten"]


def test_a_taken_team_name_is_a_400(wired):
    wired["taken_teams"].add("Team One")
    with pytest.raises(HTTPException) as e:
        _add(newTeams=[ra.NewPersonTeamIn(name="Team One")])
    assert e.value.status_code == 400
    assert "A team named Team One already exists." in str(e.value.detail)
    assert wired["cur"].sql == []
    with pytest.raises(HTTPException):
        _add(newTeams=[ra.NewPersonTeamIn(name="Same"), ra.NewPersonTeamIn(name="same")])


def test_a_dry_run_lists_new_teams(wired):
    out = _add(dryRun=True, newTeams=[ra.NewPersonTeamIn(name="Team Nine", lead=True)])
    assert out["newTeams"] == [{"name": "Team Nine", "lead": True}]
    assert out["empty"] is False and wired["cur"].sql == []


def test_the_audit_row_carries_the_new_fields(wired):
    _add(grants=[_g(tier="RW")], role=ra.NewPersonRoleIn(role="admin"),
         newTeams=[ra.NewPersonTeamIn(name="Team Nine", lead=True)])
    rows = [d for a, d in wired["audit"] if a == "person_added"]
    assert len(rows) == 1
    row = rows[0]
    assert row["grants"][0]["target_id"] == 7 and row["grants"][0]["tier"] == "rw"
    assert row["role"]["role"] == "admin"
    assert row["new_teams"] == [{"name": "Team Nine", "lead": True}]


def test_an_outside_domain_email_warns_but_still_applies(wired):
    wired["resolve"] = dict(wired["resolve"], email="jordan@other.test", title="Platform")
    out = _add(dryRun=True)
    assert out["outsideDomain"] is True and out["title"] == "Platform"
    assert {"kind": "outside_domain", "email": "jordan@other.test"} in out["warnings"]
    assert _add()["principal"] == NEW


def test_an_inside_domain_email_has_no_outside_warning(wired):
    out = _add(dryRun=True)
    assert out["outsideDomain"] is False
    assert not [w for w in out["warnings"] if w.get("kind") == "outside_domain"]


def test_the_preview_counts_auto_approve_rules_before_the_box_is_checked(wired):
    # The dialog shows the count next to the box, before anyone checks it.
    out = _add(copyFrom=REF, includeAutoApprove=False, dryRun=True)
    assert out["copy"]["autoApprove"] == 2
    preview = [b for b in wired["copy_calls"] if b.dryRun]
    assert preview and all(b.includeAutoApprove for b in preview)
    # The save still copies them only with the box.
    _add(copyFrom=REF, includeAutoApprove=False)
    saves = [b for b in wired["copy_calls"] if not b.dryRun]
    assert saves and not saves[-1].includeAutoApprove
