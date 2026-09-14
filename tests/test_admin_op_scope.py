"""Operational approvals (access-request grant, CSV import,
RO-window) enforce the approving admin's scope — not just is_admin.

The Slack handlers shape each operation as a request-like dict and pass it
through admins.can_approve (the same max_tier + target + team scope used for
query approval). These tests exercise that scope logic for those shapes with
a scoped admin whose team scope is wildcard (so no DB is touched)."""
from queryhub import admins


def _scoped(monkeypatch, *, max_tier, target_ids):
    monkeypatch.setattr(admins, "_candidate_scope_rows", lambda uid: [{
        "max_tier": max_tier,
        "scope_target_ids": target_ids,
        "scope_team_ids": None,   # wildcard -> team check skipped, no DB
        "source": "permanent",
        "grant_id": None,
    }])


def test_ro_scoped_admin_cannot_approve_ddl_import(monkeypatch):
    _scoped(monkeypatch, max_tier="ro", target_ids={7})
    # A CSV import is shaped as a DDL request on the import's target.
    imp = {"required_tier": "ddl", "target_server_id": 7,
           "requester_slack_id": "U0R"}
    assert admins.can_approve("U0ADMIN", imp) is False


def test_ro_scoped_admin_can_approve_ro_window_in_scope(monkeypatch):
    _scoped(monkeypatch, max_tier="ro", target_ids={7})
    win = {"required_tier": "ro", "target_server_id": 7,
           "requester_slack_id": "U0R"}
    assert admins.can_approve("U0ADMIN", win) is True


def test_target_scoped_admin_refused_out_of_scope_access_grant(monkeypatch):
    _scoped(monkeypatch, max_tier="ddl", target_ids={7})
    grant = {"required_tier": "rw", "target_server_id": 99,
             "requester_slack_id": "U0R"}
    assert admins.can_approve("U0ADMIN", grant) is False


def test_unscoped_admin_approves_anything(monkeypatch):
    # NULL scope columns = full wildcard admin.
    _scoped(monkeypatch, max_tier=None, target_ids=None)
    imp = {"required_tier": "ddl", "target_server_id": 123,
           "requester_slack_id": "U0R"}
    assert admins.can_approve("U0ADMIN", imp) is True
