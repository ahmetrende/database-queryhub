"""Duplicate addresses against a real database (PLA-1409).

tests/test_duplicate_addresses.py copies what `lower(btrim(email))` does. Here
the real SQL runs: the list query and the lookup queries must agree on every
seeded row.

The seed holds only duplicates that the live schema accepts. While
`access_model_mirror` is on, a write to `requesters` or `admins` copies the
address to `principal`, and `principal_email_uq` refuses a second principal
with the same `lower(email)`. Two shapes still pass that index:

- Two addresses that differ only in outer spaces. The index does not trim,
  and the lookup does.
- An admin row whose address differs from the same person's requester row.
  `principal` keeps the requester address.

Every row uses ids and a domain that no real person has. The clean-up deletes
only those rows and the principal rows that the mirror made for them.
"""
import os

import pytest

from queryhub import db, requesters

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

DOMAIN = "it-dup.example.invalid"
# The fixture inserts the rows in this order. U0ITDUP008 gets its requester
# row first, so its principal keeps "own@". Its admin row then shares
# "shared@" with U0ITDUP001.
REQUESTERS = [                          # (id, stored email, enabled)
    ("U0ITDUP001", f"shared@{DOMAIN}", True),
    ("U0ITDUP002", f"two@{DOMAIN}", True),
    ("U0ITDUP003", f"  TWO@{DOMAIN.upper()} ", False),
    ("U0ITDUP004", f"both@{DOMAIN}", True),
    ("U0ITDUP005", f"solo@{DOMAIN}", True),
    ("U0ITDUP006", "", True),
    ("U0ITDUP007", None, True),
    ("U0ITDUP008", f"own@{DOMAIN}", True),
]
ADMINS = [                              # (id, stored email)
    ("U0ITDUP008", f"shared@{DOMAIN}"),
    ("U0ITDUP004", f"BOTH@{DOMAIN}"),
    ("U0ITDUP009", "   "),
    ("U0ITDUP010", " "),
]
IDS = sorted({uid for uid, *_ in REQUESTERS} | {uid for uid, _ in ADMINS})


def _wipe():
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        # Admins first. With the requester row of U0ITDUP008 gone first, the
        # mirror would copy its admin address ("shared@") to its principal,
        # and the unique index would refuse the delete.
        cur.execute("DELETE FROM admins WHERE slack_user_id = ANY(%s)", (IDS,))
        cur.execute("DELETE FROM requesters WHERE slack_user_id = ANY(%s)", (IDS,))
        cur.execute("SELECT principal_id FROM principal_identity "
                    " WHERE provider = 'slack' AND external_id = ANY(%s)", (IDS,))
        pids = [r["principal_id"] for r in cur.fetchall()]
        for sql in ("DELETE FROM access_grant WHERE principal_id = ANY(%s)",
                    "DELETE FROM role_assignment WHERE principal_id = ANY(%s)",
                    "DELETE FROM principal_setting WHERE principal_id = ANY(%s)",
                    "DELETE FROM team_member WHERE principal_id = ANY(%s)",
                    "DELETE FROM principal_credential WHERE principal_id = ANY(%s)",
                    "DELETE FROM principal_identity WHERE principal_id = ANY(%s)",
                    "DELETE FROM principal WHERE id = ANY(%s)"):
            cur.execute(sql, (pids,))
        cur.execute("DELETE FROM auth_event_outbox WHERE slack_user_id = ANY(%s)", (IDS,))


@pytest.fixture
def seeded():
    _wipe()
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        for uid, email, enabled in REQUESTERS:
            cur.execute("INSERT INTO requesters (slack_user_id, email, name, enabled) "
                        "VALUES (%s, %s, %s, %s)", (uid, email, f"IT Dup {uid}", enabled))
        for uid, email in ADMINS:
            cur.execute("INSERT INTO admins (slack_user_id, email, name, enabled) "
                        "VALUES (%s, %s, %s, TRUE)", (uid, email, f"IT Dup Admin {uid}"))
    yield
    _wipe()


def _ours():
    return [d for d in requesters.duplicate_addresses()
            if d["email"].endswith("@" + DOMAIN)]


def test_the_list_is_exactly_what_the_lookup_calls_ambiguous(seeded):
    stored = [e for _, e, _ in REQUESTERS] + [e for _, e in ADMINS]
    ambiguous = {e.strip().lower() for e in stored
                 if e and requesters.match_email(e).ambiguous}
    listed = {d["email"] for d in _ours()}
    assert listed == ambiguous == {f"shared@{DOMAIN}", f"two@{DOMAIN}"}


def test_each_entry_names_every_row_that_shares_the_address(seeded):
    got = {d["email"]: [(r["source"], r["slack_user_id"], r["enabled"]) for r in d["rows"]]
           for d in _ours()}
    assert got == {
        f"shared@{DOMAIN}": [("requesters", "U0ITDUP001", True),
                             ("admins", "U0ITDUP008", True)],
        f"two@{DOMAIN}": [("requesters", "U0ITDUP002", True),
                          ("requesters", "U0ITDUP003", False)],
    }


def test_one_person_with_a_row_in_each_table_resolves(seeded):
    match = requesters.match_email(f"both@{DOMAIN}")
    assert not match.ambiguous and match.row["slack_user_id"] == "U0ITDUP004"
    assert requesters.principal_by_email(f"Both@{DOMAIN}")["slack_user_id"] == "U0ITDUP004"


def test_a_shared_address_is_ambiguous_and_the_old_resolver_still_says_none(seeded):
    assert requesters.match_email(f" SHARED@{DOMAIN}").ambiguous
    assert requesters.principal_by_email(f"shared@{DOMAIN}") is None
    assert requesters.match_email(f"nobody@{DOMAIN}") == (None, False)


def test_rows_without_an_address_are_never_duplicates(seeded):
    """Four seeded people have no address ('', NULL, '   ', ' '). They must
    not show up as one shared empty address."""
    assert all(d["email"] for d in requesters.duplicate_addresses())
