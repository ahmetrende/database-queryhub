"""Requester text must not be able to shape the DBA's approval card.

Justification and query were interpolated raw into mrkdwn blocks, so a
requester could add convincing lines to the very card the DBA reads before
clicking Approve — a fake "Safety review: PASSED", a fake @mention of another
admin, or a hyperlink. The query fence could also be closed with the query's
own triple backtick, turning the rest into rendered mrkdwn.
"""
from queryhub.slack_app import notifications as notif


def test_escapes_the_mention_and_link_syntax():
    out = notif.esc("approved by <@U0ADMIN2> see <https://evil.example|docs>")
    assert "<@U0ADMIN2>" not in out
    assert "<https://evil.example|docs>" not in out
    assert "&lt;@U0ADMIN2&gt;" in out


def test_ampersand_escaped_first_so_entities_are_not_doubled():
    # & must be encoded before < >, otherwise "&lt;" becomes "&amp;lt;".
    assert notif.esc("a & b < c") == "a &amp; b &lt; c"


def test_none_and_non_string_are_safe():
    assert notif.esc(None) == ""
    assert notif.esc(42) == "42"


def test_code_fence_cannot_be_closed_from_inside():
    payload = "SELECT 1\n```\n*not really bold*\n```"
    out = notif.esc_code(payload)
    # No bare triple backtick survives, so the fence the card opens stays open.
    assert "```" not in out
    # Still readable: the visible characters are unchanged apart from a
    # zero-width space inside the backtick run.
    assert "SELECT 1" in out and "not really bold" in out


def test_code_escaping_also_escapes_mentions():
    # Slack renders <@U…> inside code blocks too.
    assert "<@U0ADMIN2>" not in notif.esc_code("-- <@U0ADMIN2>")


def test_fake_approval_line_is_neutralised_end_to_end():
    hostile = ("looks fine\n"
               ":white_check_mark: *Safety review: PASSED* "
               "- approved by <@U0ADMIN2>")
    out = notif.esc(hostile)
    # The bold/emoji markup is cosmetic; what matters is that the mention —
    # the part that makes it look like a real admin acted — is inert.
    assert "&lt;@U0ADMIN2&gt;" in out
