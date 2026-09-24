"""An email resolves to one principal across BOTH people tables, or to none.

An external sign-in proves an address, and every grant, approval and audit row
hangs off a principal id. `requesters.by_email` and `admins.by_email` each
refuse two rows sharing an address inside their own table. Chained with `or`,
they did not refuse one row in EACH table naming different people: the first
table's answer won. Two callers also asked in opposite orders (requester-first
for the principal sync, admin-first for its drift report), so the same data
resolved to different principals. Measured on live data 2026-09-24: no such
collision exists, and company SSO is not yet on, so this closes it before it
can matter.
"""
import inspect

import pytest

from queryhub import requesters


def _tables(monkeypatch, req, adm):
    def fetch_all(sql, params=None):
        return list(req if "FROM requesters" in sql else adm)
    monkeypatch.setattr(requesters.db, "fetch_all", fetch_all)


def _row(uid):
    return {"slack_user_id": uid, "email": "a@example.com", "name": uid, "enabled": True}


def test_a_requester_alone_resolves(monkeypatch):
    _tables(monkeypatch, [_row("U0EXAMPLE001")], [])
    assert requesters.principal_by_email("a@example.com")["slack_user_id"] == "U0EXAMPLE001"


def test_an_admin_alone_resolves(monkeypatch):
    _tables(monkeypatch, [], [_row("U0EXAMPLE002")])
    assert requesters.principal_by_email("a@example.com")["slack_user_id"] == "U0EXAMPLE002"


def test_one_person_in_both_tables_is_one_answer_and_the_requester_row(monkeypatch):
    req = dict(_row("U0EXAMPLE001"), name="from requesters")
    _tables(monkeypatch, [req], [_row("U0EXAMPLE001")])
    assert requesters.principal_by_email("a@example.com") is req


def test_a_requester_and_a_different_admin_is_refused(monkeypatch):
    """The gap: `requesters.by_email(e) or admins.by_email(e)` returned the
    requester here, whoever the admin row belonged to."""
    _tables(monkeypatch, [_row("U0EXAMPLE001")], [_row("U0EXAMPLE002")])
    assert requesters.principal_by_email("a@example.com") is None


def test_two_rows_in_one_table_are_still_refused(monkeypatch):
    _tables(monkeypatch, [_row("U0EXAMPLE001"), _row("U0EXAMPLE003")], [])
    assert requesters.principal_by_email("a@example.com") is None


@pytest.mark.parametrize("email", ["", "   ", None])
def test_no_address_is_no_answer(monkeypatch, email):
    _tables(monkeypatch, [_row("U0EXAMPLE001")], [])
    assert requesters.principal_by_email(email) is None


def test_the_address_is_normalised_before_the_lookup(monkeypatch):
    seen = []
    monkeypatch.setattr(requesters.db, "fetch_all",
                        lambda sql, params=None: seen.append(params) or [])
    requesters.principal_by_email("  Name.Surname@Example.COM ")
    assert seen and all(p == ("name.surname@example.com",) for p in seen)


def test_every_external_sign_in_and_the_sync_resolve_through_it():
    from queryhub.web import auth_providers, idp_assertion, routes_admin
    for mod in (auth_providers, idp_assertion, routes_admin):
        src = inspect.getsource(mod)
        assert "by_email(email) or" not in src, mod.__name__
        assert "requesters.principal_by_email(email)" in src, mod.__name__
