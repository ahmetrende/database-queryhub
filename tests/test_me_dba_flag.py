"""`GET /api/me` tells the panel which admins pass the DBA level (PLA-1592 U4a).

`admin.role` is `dba` for every admin who is not a super-admin, a temporary or a
scoped one too. So it cannot say who passes `require_admin(claims, "dba")`.
`admin.isDba` says it, with the same rule as the gate.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from queryhub import admins
from queryhub.web import admin

UID = "U0EXAMPLE001"


@pytest.fixture
def who(monkeypatch):
    state = {"admin": True, "super": False, "dba": False}
    monkeypatch.setattr(admins, "is_admin", lambda uid: state["admin"])
    monkeypatch.setattr(admins, "is_super_admin", lambda uid: state["super"])
    monkeypatch.setattr(admins, "is_dba", lambda uid: state["dba"])
    monkeypatch.setattr(
        admins, "get_scope",
        lambda uid: {"permanent": {"max_tier": "ro", "scope_target_ids": None},
                     "temp_grants": []})
    return state


def test_a_super_admin_is_a_dba(who):
    who.update(super=True)
    block = admin.admin_block(UID)
    assert block["isDba"] is True and block["role"] == "super"


def test_a_permanent_unscoped_admin_is_a_dba(who):
    who.update(dba=True)
    block = admin.admin_block(UID)
    assert block["isDba"] is True and block["role"] == "dba"


def test_a_temporary_or_scoped_admin_is_not_a_dba(who):
    block = admin.admin_block(UID)
    assert block["isDba"] is False and block["role"] == "dba"


def test_a_caller_who_is_not_an_admin_gets_no_block(who):
    who.update(admin=False)
    assert admin.admin_block(UID) is None


def test_the_old_keys_keep_their_values(who):
    block = admin.admin_block(UID)
    assert {k: v for k, v in block.items() if k != "isDba"} == {
        "isAdmin": True, "role": "dba", "canApprove": ["RO"], "connections": ["*"]}
    who.update(super=True)
    assert {k: v for k, v in admin.admin_block(UID).items() if k != "isDba"} == {
        "isAdmin": True, "role": "super",
        "canApprove": ["RO", "RW", "DDL"], "connections": ["*"]}


@pytest.mark.parametrize("is_super,is_dba", [(True, False), (False, True), (False, False)])
def test_the_flag_agrees_with_the_gate(who, is_super, is_dba):
    who.update(super=is_super, dba=is_dba)
    flag = admin.admin_block(UID)["isDba"]
    try:
        admin.require_admin({"sub": UID}, "dba")
        passes = True
    except HTTPException:
        passes = False
    assert flag is passes
