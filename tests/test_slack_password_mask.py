"""A password in a submitted script must not reach Slack.

Migration 100 masks a request's stored query when the request closes. The
admin DMs are a second copy: the approval card quotes a short query inline,
a long one goes up as a .sql snippet, and a Slack message outlives the request.
A role script's password reached every approver that way. `_post` and
`_update` carry every message the notifications module sends, so the masking
is applied there, and to the two uploads that quote a request.
"""
from types import SimpleNamespace

import pytest

from queryhub import config as cfg
from queryhub import executor, query_safety
from queryhub.slack_app import notifications

MASK = query_safety.PASSWORD_MASK


@pytest.mark.parametrize("raw,want", [
    ("CREATE ROLE a WITH LOGIN PASSWORD 'gW6u-x' VALID UNTIL 'infinity'",
     f"CREATE ROLE a WITH LOGIN PASSWORD {MASK} VALID UNTIL 'infinity'"),
    ("ALTER ROLE a ENCRYPTED PASSWORD 'it''s'", f"ALTER ROLE a ENCRYPTED PASSWORD {MASK}"),
    ("alter role a password E'x\\'y' login", f"alter role a password {MASK} login"),
    ("CREATE ROLE a PASSWORD $pw$x$pw$", f"CREATE ROLE a PASSWORD {MASK}"),
    ("CREATE LOGIN a WITH PASSWORD = N'Str0ng!'", f"CREATE LOGIN a WITH PASSWORD = {MASK}"),
    # Nothing to mask: no literal, or the word is part of another name.
    ("ALTER ROLE a PASSWORD NULL", "ALTER ROLE a PASSWORD NULL"),
    ("SELECT user_password FROM t WHERE id = 'x'", "SELECT user_password FROM t WHERE id = 'x'"),
    # Masking what migration 100 already masked changes nothing.
    (f"CREATE ROLE a PASSWORD {MASK}", f"CREATE ROLE a PASSWORD {MASK}"),
])
def test_password_literals_are_masked(raw, want):
    assert query_safety.mask_password_literals(raw) == want


def test_masking_keeps_a_role_script_readable():
    sql = ("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'u') THEN\n"
           "  CREATE ROLE u WITH LOGIN PASSWORD 'secret-one';\nEND IF; END $$;\n"
           "GRANT root TO u;")
    out = query_safety.mask_password_literals(sql)
    assert "secret-one" not in out
    assert "rolname = 'u'" in out and "GRANT root TO u;" in out and "DO $$" in out


@pytest.fixture
def slack(monkeypatch):
    sent = []
    monkeypatch.setattr(cfg, "ENV", SimpleNamespace(slack_enabled=True))
    client = SimpleNamespace(
        chat_postMessage=lambda **k: sent.append(("post", k)) or {"ts": "1"},
        chat_update=lambda **k: sent.append(("update", k)) or {"ts": "1"},
        files_upload_v2=lambda **k: sent.append(("upload", k)) or {"files": [{"id": "F1"}]})
    return client, sent


def test_a_posted_or_updated_message_carries_no_password(slack):
    client, sent = slack
    blocks = [{"type": "section", "text": {"type": "mrkdwn",
               "text": "```CREATE ROLE a PASSWORD 'secret-one'```"}}]
    notifications._post(client, channel="D1", text="PASSWORD 'secret-two'", blocks=blocks)
    notifications._update(client, channel="D1", ts="1", text="x", blocks=blocks)
    flat = repr(sent)
    assert "secret-one" not in flat and "secret-two" not in flat
    assert MASK in flat


def test_the_sql_snippet_carries_no_password(slack):
    client, sent = slack
    notifications._upload_query_snippet(client, "D1", 7, "CREATE ROLE a PASSWORD 'secret-one'", "1")
    assert sent[0][0] == "upload" and "secret-one" not in sent[0][1]["content"]


def test_a_result_upload_comment_carries_no_password(slack):
    client, sent = slack
    executor._upload_with_retry(client, channel="D1", file="x.csv",
                                initial_comment="done: CREATE ROLE a PASSWORD 'secret-one'")
    assert "secret-one" not in sent[0][1]["initial_comment"]


def test_the_manual_run_card_says_the_password_is_hidden(monkeypatch):
    """Whoever runs a masked role script by hand needs a password; the card
    says to set one rather than leaving them to find `***REDACTED***`."""
    monkeypatch.setattr(notifications, "_request_blocks", lambda r, t: [{"type": "section"}, {"type": "actions"}])
    t = SimpleNamespace(alias="ledger")
    with_pw = notifications._dba_manual_blocks(
        {"id": 1, "query": "CREATE ROLE a PASSWORD 'x'"}, t, "line")
    without = notifications._dba_manual_blocks({"id": 2, "query": "ALTER TABLE t ADD c int"}, t, "line")
    assert "password in this script is hidden" in repr(with_pw)
    assert "hidden" not in repr(without)
