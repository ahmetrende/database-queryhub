"""Nobody approves their own write from the MCP door. An admin is no exception.

The door sends every RW or DDL request to a person (`human_only`). If the admin
who asked Claude for the write could approve it, the only reader would be the
person who asked for it. A second person must read what the model wrote. The
other channels keep the admin exemption.

Nothing here may reach a database. A read a test forgot to fake fails it.
"""
from __future__ import annotations

import pytest

from queryhub import access, admins, db

ME = "U0EXAMPLE001"
OTHER = "U0EXAMPLE002"


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)


@pytest.fixture
def admin(monkeypatch):
    """Every caller is an unscoped admin in the new model."""
    monkeypatch.setattr(admins, "_v2", lambda: True)
    monkeypatch.setattr(access, "roles", lambda pid: [{
        "role": "admin", "scope_team_id": None, "all_teams": True,
        "scope_target_id": None, "all_targets": True, "max_tier": None,
        "any_tier": True, "valid_until": None}])


def _req(tier, origin="mcp", requester=ME):
    return {"id": 41, "requester_slack_id": requester, "target_server_id": 5,
            "required_tier": tier, "engine": "postgres",
            "query": "UPDATE ledgers SET n = 1 WHERE id = 2", "origin": origin}


@pytest.mark.parametrize("tier", ["rw", "ddl"])
def test_an_admin_cannot_approve_their_own_mcp_write(admin, tier):
    assert admins.can_approve(ME, _req(tier)) is False


@pytest.mark.parametrize("tier", ["rw", "ddl"])
def test_another_admin_can_approve_it(admin, tier):
    assert admins.can_approve(OTHER, _req(tier)) is True


def test_an_admin_can_approve_their_own_mcp_read(admin):
    assert admins.can_approve(ME, _req("ro")) is True


@pytest.mark.parametrize("origin", ["slack", "web", "idp", None])
def test_other_channels_keep_the_admin_exemption(admin, origin):
    assert admins.can_approve(ME, _req("rw", origin=origin)) is True


def test_the_origin_is_normalized_before_the_compare(admin):
    assert admins.can_approve(ME, _req("rw", origin=" MCP ")) is False


def test_a_row_without_origin_reads_the_stored_origin(admin, monkeypatch):
    """Some callers pass a short row. The rule reads the stored origin, so a
    short row cannot skip it."""
    seen = []

    def fetch_one(sql, params):
        seen.append((sql, params))
        return {"origin": "mcp"}
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    row = _req("rw")
    del row["origin"]
    assert admins.can_approve(ME, row) is False
    assert len(seen) == 1
    assert "FROM requests" in seen[0][0] and seen[0][1] == (41,)


def test_a_row_without_origin_or_id_is_not_a_query_request(admin):
    """An access request or a window request, in the shape of a request row.
    These never come from the MCP door, so the admin exemption stays."""
    row = {"requester_slack_id": ME, "target_server_id": 5, "required_tier": "rw"}
    assert admins.can_approve(ME, row) is True


def test_the_legacy_model_refuses_it_too(monkeypatch):
    monkeypatch.setattr(admins, "_v2", lambda: False)
    monkeypatch.setattr(admins, "_candidate_scope_rows", lambda uid: [{
        "scope_team_ids": None, "scope_target_ids": None, "max_tier": None}])
    assert admins.can_approve(ME, _req("rw")) is False
    assert admins.can_approve(OTHER, _req("rw")) is True
