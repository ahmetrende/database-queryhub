"""The confirmation-required 409 answers with a code of its own.

Design's ask, and the reason for it is a live misfire rather than tidiness:
the client has to tell "answer this question" from "you already sent this",
and while both were `conflict` it could only do so by matching message text.
Measured 2026-08-14, the duplicate wording did not match the client's
duplicate regex — so a duplicate was being answered with a confirm dialog
whose confirmation the server then refused again.
"""
import pytest

from queryhub import core_submit
from queryhub.web import deps, routes_queries


def _raise(rej):
    with pytest.raises(deps.HTTPException) as ei:
        routes_queries._reject(rej)
    return ei.value


def test_needs_confirmation_gets_its_own_code():
    e = _raise(core_submit.Rejection("confirm", "This drops a table.",
                                     reason="needs_confirmation"))
    assert e.status_code == 409
    assert e.detail["code"] == "confirmation_required"


def test_a_duplicate_is_still_a_plain_conflict():
    """The two must not share a code — that sharing is the whole bug."""
    e = _raise(core_submit.Rejection(
        "query", "You already have an active request (#5, status=pending).",
        reason="duplicate"))
    assert e.status_code == 409
    assert e.detail["code"] == "conflict"


def test_a_rate_limit_is_still_a_plain_conflict():
    e = _raise(core_submit.Rejection("rate_limit", "5 in flight."))
    assert e.detail["code"] == "conflict"


def test_the_reasons_come_back_as_a_list():
    """One sentence per statement, in statement order — the modal renders one
    block each and does not renumber them, so the order is ours to get right."""
    e = _raise(core_submit.Rejection(
        "confirm", "a b", reason="needs_confirmation",
        reasons=("DROP TABLE users — every row is lost.",
                 "UPDATE with no WHERE — rewrites every row.")))
    assert e.detail["reasons"] == [
        "DROP TABLE users — every row is lost.",
        "UPDATE with no WHERE — rewrites every row.",
    ]


def test_slack_emphasis_is_stripped_from_reasons_too():
    """The web contract never carries Slack mrkdwn; the message already had
    that treatment and the list has to get the same."""
    e = _raise(core_submit.Rejection(
        "confirm", "*x*", reason="needs_confirmation",
        reasons=("*DROP TABLE users* — every row is lost.",)))
    assert "*" not in e.detail["reasons"][0]


def test_an_error_with_no_reasons_carries_no_empty_key():
    """Every other error's body stays byte-identical to before."""
    e = _raise(core_submit.Rejection("query", "Provide your SQL query."))
    assert "reasons" not in e.detail
    assert set(e.detail) == {"code", "message"}


def test_the_envelope_still_has_code_and_message_first():
    e = deps._error(422, "validation", "nope")
    assert e.detail == {"code": "validation", "message": "nope"}
