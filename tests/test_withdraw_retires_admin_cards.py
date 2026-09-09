"""A withdrawn request comes off the admins' queue, from either surface.

Reported from a real card: request #7368 still showed Approve / Reject after
its requester withdrew it, and pressing them answered "already been decided" --
four times, because a button that answers with a contradiction reads as a
broken button rather than a request that is gone.

The cause was the usual shape of this codebase's bugs: two surfaces, one
behaving. The Slack withdraw handler has always updated every admin card and
the requester's card. The web route called `cancellation.withdraw`, which is a
database function with no Slack client -- correctly so -- and then returned,
leaving the messages nobody had sent. Its own docstring claimed otherwise:
"close the row, leave the audit line, and (for a pending one) take it off the
admins' queue".

An intent written in a comment and not in the code is worse than an omission,
because the next reader stops looking.
"""
import inspect

from queryhub import cancellation
from queryhub.web import routes_queries


def test_the_web_withdraw_retires_the_cards():
    assert "_retire_admin_cards(row, claims)" in inspect.getsource(
        routes_queries.query_cancel)


def test_it_updates_both_the_admin_cards_and_the_requester_card():
    """The requester's own card is the other half: it still said "waiting for
    an admin" about a request they had just withdrawn themselves."""
    src = inspect.getsource(routes_queries._retire_admin_cards)
    assert "update_all_admin_messages" in src
    assert "update_requester_card" in src


def test_a_bundle_item_uses_the_bundle_updater():
    """A bundle's admin DM is one message listing every item, so rewriting it
    per item would fight itself. Mirrors what the edit-and-resubmit path
    already does."""
    src = inspect.getsource(routes_queries._retire_admin_cards)
    assert 'row.get("bundle_id")' in src
    assert "update_bundle_admin_dms" in src


def test_a_slack_failure_does_not_undo_a_committed_withdrawal():
    """The row is already updated and the caller is about to be told it
    worked. Raising here would report a failure for something that happened."""
    src = inspect.getsource(routes_queries._retire_admin_cards)
    assert "except Exception:" in src
    assert "log.exception" in src


def test_it_is_a_no_op_without_slack():
    """The vanilla profile has no Slack client at all; there are no cards to
    retire and no client to raise on."""
    src = inspect.getsource(routes_queries._retire_admin_cards)
    assert "if client is None:" in src


def test_withdraw_itself_stays_a_database_function():
    """It is called from both surfaces and inside a transaction. Reaching for
    a Slack client from there would put network I/O in a database function and
    make the two callers' failure modes different again."""
    src = inspect.getsource(cancellation.withdraw)
    for forbidden in ("notifications", "WebClient", "client"):
        assert forbidden not in src, forbidden


def test_the_status_line_says_no_action_is_needed():
    """An admin reading the card should not have to work out whether the
    absence of buttons means "already handled by someone" or "gone"."""
    assert "no action needed" in inspect.getsource(
        routes_queries._retire_admin_cards)
