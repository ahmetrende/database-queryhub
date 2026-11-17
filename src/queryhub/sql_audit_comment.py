"""A comment in front of each SQL statement that QueryHub sends to a target.

Some managed database services write an audit log that keeps only the
statement text. One such service writes the session's `application_name` as
"0" and does not let us change its log line prefix. So the request id and the
requester that the executor puts in `application_name` do not reach that log.
The statement text does. This module makes the comment that carries them:

    /* QueryHub (web) req_id=5369 by=jordan.ray@example.com */ SELECT ...

The comment goes at the START of the text, because an audit log can cut a
long statement at its end.

The bot_config key `sql_audit_comment` turns it on. The default is off. The
app reads the key for each request, so a change needs no restart.

Rules:

* Only the text sent to the target changes. `requests.query` stays as the
  requester wrote it.
* The executor adds the comment after the safety checks and after it puts the
  password back into a statement. The checks never see the comment.
* The comment and the statement are on the same line, so the line numbers in
  an error message do not change.
* Every value in the comment keeps only `A-Z a-z 0-9 @ . _ + -`. Each other
  character becomes `_`. A value cannot close the comment.
* On SQL Server, the text of a procedure, function, view or trigger includes
  everything in its batch before the CREATE. So for those statements the
  comment goes in its own batch, just before the statement, on the same
  connection. `GO` is a client command, and QueryHub never sends it.
"""
from __future__ import annotations

import logging
import re

from . import config as cfg
from . import origins, profile_sync

log = logging.getLogger(__name__)

SETTING = "sql_audit_comment"

_UNSAFE = re.compile(r"[^A-Za-z0-9@._+-]")
_MAX_VALUE = 120

# A SQL Server statement that creates or changes a module. SQL Server keeps
# the text of the whole batch as the module's definition.
_MSSQL_MODULE = re.compile(
    r"(?:CREATE(?:\s+OR\s+ALTER)?|ALTER)\s+"
    r"(?:PROC|PROCEDURE|FUNCTION|VIEW|TRIGGER)\b",
    re.IGNORECASE)


def enabled() -> bool:
    """True when bot_config `sql_audit_comment` is on."""
    return cfg.get_bool(SETTING, False)


def _clean(value) -> str:
    text = _UNSAFE.sub("_", str(value if value is not None else ""))
    return text[:_MAX_VALUE] or "unknown"


def _who(principal: str | None) -> str:
    """The requester's email, or the principal id when there is no email."""
    if not principal:
        return "unknown"
    try:
        email = profile_sync.lookup_email(principal)
    except Exception:
        log.warning("sql audit comment: email lookup failed for %s", principal,
                    exc_info=True)
        email = None
    return email or principal


def comment(*, origin: str | None, request_id, principal: str | None) -> str:
    """The comment, without a trailing space. It ignores the switch.

    `request_id` is None before the request row exists, for example in the
    pre-flight EXPLAIN at submit time. The comment then says `req_id=none`.
    """
    rid = _clean(request_id) if request_id is not None else "none"
    return (f"/* QueryHub ({_clean(origins.normalize(origin))}) "
            f"req_id={rid} by={_clean(_who(principal))} */")


def prefix(*, origin: str | None, request_id=None,
           principal: str | None = None) -> str:
    """The comment and one space, or "" when the switch is off."""
    if not enabled():
        return ""
    return comment(origin=origin, request_id=request_id,
                   principal=principal) + " "


def for_request(request: dict) -> str:
    """`prefix()` for a request row."""
    return prefix(origin=request.get("origin"), request_id=request.get("id"),
                  principal=request.get("requester_slack_id"))


def _strip_leading_comments(sql: str) -> str:
    """`sql` without its leading spaces and comments. T-SQL block comments
    can nest, so the code counts their depth."""
    s = sql
    while True:
        s = s.lstrip()
        if s.startswith("--"):
            end = s.find("\n")
            if end < 0:
                return ""
            s = s[end + 1:]
        elif s.startswith("/*"):
            depth, i = 0, 0
            while i < len(s):
                if s.startswith("/*", i):
                    depth += 1
                    i += 2
                elif s.startswith("*/", i):
                    depth -= 1
                    i += 2
                    if depth == 0:
                        break
                else:
                    i += 1
            if depth:
                return ""
            s = s[i:]
        else:
            return s


def needs_own_batch(sql: str, engine: str | None) -> bool:
    """True when the comment must go in its own batch, before `sql`.

    Only for SQL Server, and only for a statement that creates or changes a
    procedure, function, view or trigger.
    """
    if (engine or "postgres") != "mssql":
        return False
    return bool(_MSSQL_MODULE.match(_strip_leading_comments(sql or "")))
