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
