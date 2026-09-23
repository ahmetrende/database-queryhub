"""Keep a submitted statement's password out of the bot's database.

`requests.query` is read by every screen, DM, export and report, and a role
script carries the new role's password as a literal. Migration 100 masked it
when the request closed, so a password sat in cleartext for as long as the
request was open: seconds for an auto-approved run, days for one waiting on an
approver or on a DBA to run it by hand.

So `query` holds the statement with its passwords masked from the moment it is
written. The executor still has to send the real statement, so the original is
kept beside it in `requests.query_secret`, encrypted with the master key as
target credentials are, and only while the request can still run: migration
129's trigger drops it when the request ends, and masks `query` on every write
in case a path written later forgets to.

A request that reaches a DBA to run by hand keeps no copy either. The DBA sees
the masked script and sets a new password; the Slack card says so.
"""
from __future__ import annotations

from . import crypto, db, query_safety


def split(sql: str) -> tuple[str, str | None]:
    """(the text to store in `query`, the encrypted original or None).

    None when there is nothing to hide, so an ordinary statement is stored as
    it was written and carries no secret at all.
    """
    masked = query_safety.mask_password_literals(sql)
    if masked == sql:
        return sql, None
    return masked, crypto.encrypt(sql)


class SecretGone(Exception):
    """The statement had a password and its encrypted copy is gone."""


def statement_to_run(request: dict) -> str:
    """The statement the executor sends for `request`.

    The stored text shows whether anything was hidden: `split` masks exactly
    when it keeps a secret, so a statement with no mask in it has none, and is
    run as stored without another read. A masked one is completed from the
    row's encrypted copy -- read now, because only the row knows whether the
    copy still exists.
    """
    query = request.get("query") or ""
    if not query_safety.has_masked_password(query):
        return query
    row = db.fetch_one("SELECT query_secret FROM requests WHERE id = %s",
                       (request["id"],))
    token = (row or {}).get("query_secret")
    if token:
        return crypto.decrypt(token)
    # Sending the masked text would set `***REDACTED***` as somebody's password.
    raise SecretGone()
