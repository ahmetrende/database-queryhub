"""An approver approves RO or RW, never DDL (K7, 2026-10-24).

Two app paths write an approver's tier ceiling: a DBA scope (`/admin/scopes`)
and a role `approver` (`/admin/roles`). Both refuse DDL with HTTP 400 and the
code `tier_not_allowed`. A role `admin` is not an approver here: with no
ceiling it is a super-admin, and a super-admin approves DDL.

The database has no CHECK for this. A row written with SQL still works, and a
test here pins that the rule lives in the routes and in no migration.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from queryhub import approver_limits
from queryhub.web import routes_admin as ra

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "src" / "queryhub" / "web" / "routes_admin.py").read_text(encoding="utf-8")


def _code(exc: HTTPException) -> str:
    return exc.detail["code"]


# --- the two checks -----------------------------------------------------------


@pytest.mark.parametrize("tier", ["ro", "rw", "RO", "RW"])
def test_ro_and_rw_pass(tier):
    approver_limits.check_scope_tier(tier)
    approver_limits.check_approver_ceiling(tier)


@pytest.mark.parametrize("tier", ["ddl", "DDL", None, ""])
def test_ddl_and_no_tier_are_refused_with_the_code(tier):
    with pytest.raises(approver_limits.TierRefused) as scope:
        approver_limits.check_scope_tier(tier)
    with pytest.raises(approver_limits.TierRefused) as role:
        approver_limits.check_approver_ceiling(tier)
    assert scope.value.code == role.value.code == "tier_not_allowed"
    assert "Schema changes need a super-admin." in scope.value.message
    assert "Schema changes need a super-admin." in role.value.message


# --- the role route -----------------------------------------------------------


@pytest.mark.parametrize("tier", ["ro", "RW", "Rw"])
def test_an_approver_role_with_ro_or_rw_is_accepted(tier):
    assert ra._check_role_spec("approver", None, None, tier) == tier.lower()


@pytest.mark.parametrize("tier", ["ddl", "DDL", None, ""])
def test_an_approver_role_with_ddl_or_no_ceiling_is_refused(tier):
    with pytest.raises(HTTPException) as exc:
        ra._check_role_spec("approver", None, None, tier)
    assert exc.value.status_code == 400
    assert _code(exc.value) == "tier_not_allowed"


def test_a_role_admin_and_a_granter_are_not_touched_by_the_approver_rule():
    # An admin with no ceiling is a super-admin: no refusal, as before.
    assert ra._check_role_spec("admin", None, None, None) is None
    assert ra._check_role_spec("granter", None, None, None) is None


def test_an_unknown_tier_still_gets_its_own_code_first():
    with pytest.raises(HTTPException) as exc:
        ra._check_role_spec("approver", None, None, "root")
    assert _code(exc.value) == "tier_scope"


# --- the scope route ----------------------------------------------------------


def _scope(monkeypatch, **body):
    """Run `_apply_scope` with no database. A call that gets past the tier check
    would reach the transaction, which fails the test."""
    monkeypatch.setattr(ra.admins, "is_super_admin", lambda _u: False)

    def no_db(*_a, **_k):
        raise AssertionError("the route reached the database")

    monkeypatch.setattr(ra.db, "transaction", no_db)
    model = ra.ScopeIn(admin="U0EXAMPLE002", **body)
    return ra._apply_scope(model, "U0EXAMPLE001", "Jordan Ray")


@pytest.mark.parametrize("tiers", [["DDL"], ["RO", "RW", "DDL"], ["ro", "ddl"]])
def test_a_dba_scope_with_ddl_is_refused_before_any_write(monkeypatch, tiers):
    with pytest.raises(HTTPException) as exc:
        _scope(monkeypatch, role="dba", canApprove=tiers, connections=[])
    assert exc.value.status_code == 400
    assert _code(exc.value) == "tier_not_allowed"


def test_a_dba_scope_with_ro_and_rw_reaches_the_write(monkeypatch):
    with pytest.raises(AssertionError, match="reached the database"):
        _scope(monkeypatch, role="dba", canApprove=["RO", "RW"], connections=[])


def test_a_super_admin_scope_needs_no_tier(monkeypatch):
    with pytest.raises(AssertionError, match="reached the database"):
        _scope(monkeypatch, role="super", canApprove=[], connections=[])


# --- the rule sits in the routes and nowhere else ------------------------------


def _func(name: str) -> str:
    m = re.search(rf"\ndef {name}\(.*?(?=\n@router\.|\ndef |\nclass |\Z)", SRC, re.S)
    assert m, f"{name} is gone"
    return m.group(0)


def test_each_writer_of_an_approver_ceiling_calls_the_module():
    assert "approver_limits.check_scope_tier(" in _func("_apply_scope")
    assert "approver_limits.check_approver_ceiling(" in _func("_check_role_spec")


def test_the_scope_check_runs_before_the_transaction():
    body = _func("_apply_scope")
    assert body.index("check_scope_tier(") < body.index("db.transaction()")


def test_no_migration_forbids_a_ddl_ceiling():
    # The back door is deliberate: a row written with SQL keeps working.
    for path in sorted((ROOT / "migrations").glob("*.sql")):
        text = path.read_text(encoding="utf-8").lower()
        assert not re.search(r"check\s*\([^)]*max_tier[^)]*<>\s*'ddl'", text), path.name
        assert not re.search(r"check\s*\([^)]*max_tier[^)]*in\s*\(\s*'ro'\s*,\s*'rw'\s*\)", text), path.name
