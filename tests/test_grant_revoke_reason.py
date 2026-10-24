"""An admin can give an optional reason when a grant is revoked.

The reason goes into the audit row only. A DELETE with no body keeps working.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from queryhub import grants
from queryhub.web import routes_admin as ra

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Jordan Ray"}


class Cur:
    def __init__(self, row):
        self.row = row
        self.sql: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []


class Txn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *exc):
        return False


@pytest.fixture
def env(monkeypatch):
    st = {"audit": [], "revoke": []}
    row = {"id": 9, "principal_id": 1, "team_id": None, "auto_approve": True,
           "mirrored_from": None}
    cur = Cur(row)
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need: claims["sub"])
    monkeypatch.setattr(ra, "_require_super", lambda uid, what: None)
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn(cur))
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda c, rid, uid, name, action, details=None:
                        st["audit"].append((action, details)))
    monkeypatch.setattr(ra.grants, "revoke",
                        lambda **kw: st["revoke"].append(kw) or {"mode": "ro"})
    return st


def test_fleet_revoke_with_reason_writes_it_to_the_audit(env):
    ra.admin_revoke_fleet_grant(9, claims=CLAIMS, body=ra.RevokeIn(reason="  left the team  "))
    assert env["audit"] == [("fleet_grant_revoked",
                             {"ids": [9], "reason": "left the team"})]


def test_fleet_revoke_without_body_has_no_reason_key(env):
    ra.admin_revoke_fleet_grant(9, claims=CLAIMS, body=None)
    (action, details), = env["audit"]
    assert action == "fleet_grant_revoked" and "reason" not in details


def test_fleet_revoke_empty_reason_counts_as_none(env):
    ra.admin_revoke_fleet_grant(9, claims=CLAIMS, body=ra.RevokeIn(reason="   "))
    assert "reason" not in env["audit"][0][1]


def test_fleet_revoke_long_reason_is_400_and_writes_nothing(env):
    with pytest.raises(HTTPException) as e:
        ra.admin_revoke_fleet_grant(9, claims=CLAIMS, body=ra.RevokeIn(reason="x" * 501))
    assert e.value.status_code == 400
    assert env["audit"] == []


def test_fleet_revoke_500_characters_is_accepted(env):
    ra.admin_revoke_fleet_grant(9, claims=CLAIMS, body=ra.RevokeIn(reason="x" * 500))
    assert len(env["audit"][0][1]["reason"]) == 500


def test_user_grant_revoke_passes_the_reason_on(env):
    ra.admin_delete_grant("u:U0EXAMPLE002:5", claims=CLAIMS, body=ra.RevokeIn(reason="project over"))
    assert env["revoke"][0]["reason"] == "project over"


def test_user_grant_revoke_without_body_passes_none(env):
    ra.admin_delete_grant("u:U0EXAMPLE002:5", claims=CLAIMS, body=None)
    assert env["revoke"][0]["reason"] is None


def test_user_grant_long_reason_is_400_before_any_revoke(env):
    with pytest.raises(HTTPException) as e:
        ra.admin_delete_grant("u:U0EXAMPLE002:5", claims=CLAIMS, body=ra.RevokeIn(reason="x" * 501))
    assert e.value.status_code == 400
    assert env["revoke"] == []


def test_grants_revoke_audit_has_reason_key_only_when_present(monkeypatch):
    import json
    seen = []
    cur = Cur({"slack_user_id": "U0EXAMPLE002", "target_server_id": 5, "mode": "ro"})
    monkeypatch.setattr(grants.db, "transaction", lambda: Txn(cur))
    for reason in ("project over", None):
        cur.sql.clear()
        grants.revoke(granter_id="U0EXAMPLE001", granter_name="Jordan Ray",
                      grantee_id="U0EXAMPLE002", target_id=5, reason=reason,
                      notify=False)
        seen.append(json.loads(cur.sql[-1][1][2]))
    assert seen[0]["reason"] == "project over"
    assert "reason" not in seen[1]
