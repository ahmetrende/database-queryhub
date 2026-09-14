"""Submitting a database that does not exist on the server named with it.

Request 7596: a database belonging to a DIFFERENT server reached submit —
picked on one target, the target then switched, submitted against the second.
Three things had to be true at once for it to reach production and fail:

  1. the modal handed the submit the wrong database (still open — the first
     fix for it was aimed at an inferred mechanism, and this request went
     through anyway, so the reader now logs what Slack actually sends);
  2. nothing checked the database against the target. The catalog knew;
     `auto_approve.validate_scope` was already written to answer exactly this
     and was only wired to auto-approve grants;
  3. pre-flight failed OPEN on the connect error, because
     `database "..." does not exist` arrives as an OperationalError and that
     branch treats every OperationalError as a transport problem.

(2) and (3) are the guards. They are independent of (1) on purpose: whatever
the modal does, a request naming a database its server does not have must not
reach an approver — this one cost 45 minutes of one and then failed anyway.
"""
from __future__ import annotations

import pytest

from queryhub import pre_flight


# ---------------------------------------------------------------------------
# pre-flight stops failing open when the answer is permanent
# ---------------------------------------------------------------------------

class _Err(Exception):
    """Stands in for psycopg.OperationalError. `sqlstate` is None on purpose:
    measured on this build, psycopg leaves it unset for a failure raised
    during connect, which is why the detector reads the message."""
    sqlstate = None


def test_a_missing_database_is_recognised():
    assert pre_flight._is_missing_database(_Err(
        'connection failed: connection to server at "db.example", port 5432 '
        'failed: FATAL:  database "somedb" does not exist'))


def test_the_sqlstate_is_still_honoured_if_a_later_psycopg_sets_it():
    e = _Err("something opaque")
    e.sqlstate = "3D000"
    assert pre_flight._is_missing_database(e)


@pytest.mark.parametrize("msg", [
    "connection timed out",
    "could not translate host name to address",
    'FATAL:  password authentication failed for user "queryhub_ro"',
    "server closed the connection unexpectedly",
    "FATAL:  too many connections for role",
])
def test_real_transport_and_credential_failures_still_fail_open(msg):
    """The whole point of the open door: a requester is not punished for our
    network or our bad credential. Only the impossible request is refused."""
    assert not pre_flight._is_missing_database(_Err(msg))


def test_a_relation_that_does_not_exist_is_not_mistaken_for_a_database():
    """Different error, different branch — this one already surfaced through
    the SQL-level handler and must not be caught here."""
    assert not pre_flight._is_missing_database(
        _Err('ERROR:  relation "sessions" does not exist'))
