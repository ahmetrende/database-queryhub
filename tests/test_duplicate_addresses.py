"""Addresses that two people share: the list for admins (PLA-1409, item 4b).

The IdP assertion refuses an address that rows of two DIFFERENT people share
(`ambiguous_address`). `GET /api/admin/people/duplicate-addresses` lists those
addresses for a super-admin. It must list exactly the addresses that
`requesters.match_email` calls ambiguous. An extra entry sends an admin after a
problem that does not exist. A missing entry leaves a refusal that nobody can
explain.

The two people tables live in memory here. `_pg_key` copies what PostgreSQL's
`lower(btrim(email))` does to a stored address: `btrim` removes spaces only.
tests/test_integration_duplicate_addresses.py runs the real SQL.
"""
import pytest
from fastapi.testclient import TestClient

from queryhub import admins, requesters
from queryhub.web import app as web_app
from queryhub.web import sessions


def _pg_key(email):
    return None if email is None else email.strip(" ").lower()


def _row(uid, email, name=None, enabled=True):
    return {"slack_user_id": uid, "email": email, "name": name, "enabled": enabled}


class People:
    """requesters + admins, answering the queries that requesters.py sends.

    The list query gets every row, also the rows with no address. The real
    SQL leaves those out, and the Python side must not depend on it.
    """

    def __init__(self, req, adm):
        self.tables = {"requesters": req, "admins": adm}
        self.calls = 0

    def fetch_all(self, sql, params=None):
        self.calls += 1
        if "UNION ALL" in sql:                       # duplicate_addresses()
            return [{"address": _pg_key(r["email"]), "source": table,
                     "slack_user_id": r["slack_user_id"], "name": r["name"],
                     "enabled": r["enabled"]}
                    for table in ("requesters", "admins") for r in self.tables[table]]
        table = "requesters" if "FROM requesters" in sql else "admins"   # match_email()
        return [r for r in self.tables[table] if _pg_key(r["email"]) == params[0]]


# One data set with every case in it.
REQUESTERS = [
    _row("U0REQ00001", "shared@example.com", "Req One"),   # + another person's admin row
    _row("U0REQ00002", "two@example.com", "Req Two"),      # + another requester
    _row("U0REQ00003", "  TWO@Example.com ", None, False),  # the same key after lower(btrim)
    _row("U0BOTH0001", "both@example.com", "Both"),        # the same person in admins
    _row("U0REQ00004", "solo@example.com", "Solo"),
    _row("U0REQ00005", "", "Empty"),
    _row("U0REQ00006", None, "Null"),
    _row("U0REQ00007", "\ttab@example.com", "Tab"),        # a key that no lookup reaches
]
ADMINS = [
    _row("U0ADM00001", "shared@example.com", "Adm One"),
    _row("U0BOTH0001", "BOTH@example.com", "Both"),
    _row("U0ADM00002", "", "Empty Admin"),
    _row("U0ADM00003", "   ", "Blank Admin"),
    _row("U0ADM00004", "\ttab@example.com", "Tab Admin"),
]


@pytest.fixture
def people(monkeypatch):
    p = People(list(REQUESTERS), list(ADMINS))
    monkeypatch.setattr(requesters.db, "fetch_all", p.fetch_all)
    return p


def test_the_list_is_exactly_what_the_lookup_calls_ambiguous(people):
    listed = {d["email"] for d in requesters.duplicate_addresses()}
    # Every address that a sign-in could carry for these rows: the stored
    # value, its database key and its normalised form.
    candidates = set()
    for rows in people.tables.values():
        for r in rows:
            if r["email"] is not None:
                candidates |= {r["email"], _pg_key(r["email"]), r["email"].strip().lower()}
    ambiguous = {c.strip().lower() for c in candidates
                 if requesters.match_email(c).ambiguous}
    assert listed == ambiguous
    assert listed == {"shared@example.com", "two@example.com"}


def test_one_person_with_a_row_in_each_table_is_not_a_duplicate(people):
    assert "both@example.com" not in {d["email"] for d in requesters.duplicate_addresses()}
    assert requesters.match_email("both@example.com").row["slack_user_id"] == "U0BOTH0001"


def test_rows_without_an_address_are_never_duplicates(people):
    """Four people have no address: '', NULL and '   '. Sharing "nothing" is
    not sharing an address."""
    assert all(d["email"] for d in requesters.duplicate_addresses())


def test_each_entry_names_every_row_that_shares_the_address(people):
    got = {d["email"]: [(r["source"], r["slack_user_id"], r["enabled"]) for r in d["rows"]]
           for d in requesters.duplicate_addresses()}
    assert got == {
        "shared@example.com": [("requesters", "U0REQ00001", True),
                               ("admins", "U0ADM00001", True)],
        "two@example.com": [("requesters", "U0REQ00002", True),
                            ("requesters", "U0REQ00003", False)],
    }


def test_principal_by_email_still_answers_none_for_a_listed_address(people):
    """The old resolver keeps its answer. OIDC and the sync see no change."""
    for d in requesters.duplicate_addresses():
        assert requesters.principal_by_email(d["email"]) is None


# ---------------------------------------------------------------------------
# The route: super-admin only, and its JSON shape
# ---------------------------------------------------------------------------

URL = "/api/admin/people/duplicate-addresses"


@pytest.fixture
def client_as(monkeypatch, people):
    """A real app with a valid session for `uid`. Only the role varies."""
    def make(uid, *, admin, super_admin):
        monkeypatch.setattr(sessions, "verify_access",
                            lambda t: {"sub": uid, "sid": "s1", "provider": "slack"}
                            if t == "good" else None)
        monkeypatch.setattr(sessions, "session_alive", lambda sid, principal=None: True)
        monkeypatch.setattr(admins, "is_admin", lambda u: admin)
        monkeypatch.setattr(admins, "is_super_admin", lambda u: super_admin)
        client = TestClient(web_app.create_app())
        client.cookies.set("qh_session", "good")
        return client
    return make


def test_a_super_admin_gets_the_list(client_as):
    r = client_as("U0SUPER001", admin=True, super_admin=True).get(URL)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "duplicates": [
            {"email": "shared@example.com", "accounts": [
                {"table": "requesters", "principal": "U0REQ00001", "name": "Req One",
                 "enabled": True},
                {"table": "admins", "principal": "U0ADM00001", "name": "Adm One",
                 "enabled": True}]},
            {"email": "two@example.com", "accounts": [
                {"table": "requesters", "principal": "U0REQ00002", "name": "Req Two",
                 "enabled": True},
                {"table": "requesters", "principal": "U0REQ00003", "name": "",
                 "enabled": False}]},
        ],
        "total": 2,
    }


def test_an_empty_list_is_an_empty_list(client_as, people):
    people.tables["admins"] = []
    people.tables["requesters"] = [_row("U0REQ00001", "a@example.com")]
    r = client_as("U0SUPER001", admin=True, super_admin=True).get(URL)
    assert r.status_code == 200
    assert r.json() == {"duplicates": [], "total": 0}


@pytest.mark.parametrize("admin,message", [
    (True, "Refused: you need super-admin access."),     # an admin, not a super-admin
    (False, "Refused: you need admin access."),           # a plain user
])
def test_everyone_else_is_refused_before_the_query_runs(client_as, people, admin, message):
    r = client_as("U0OTHER001", admin=admin, super_admin=False).get(URL)
    assert r.status_code == 403
    assert r.json() == {"error": {"code": "forbidden", "message": message}}
    assert people.calls == 0
