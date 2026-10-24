"""Outbox rows for the IdP panel: access requests and auto-approve windows.

The panel reads `notification_outbox` (migration 102) through
`GET /api/admin/notifications/outbox`. `core_submit._write_outbox_row` writes
`queryhub.request_pending`. This module writes the four events of the two
other request kinds:

    queryhub.access_request_pending    a person asks for access to a database
    queryhub.access_request_decided    an admin approves or rejects that request
    queryhub.window_request_pending    a person asks for an auto-approve window
    queryhub.window_request_decided    an admin approves or rejects that request

The rules are the rules of `queryhub.request_pending`:

- A caller writes a row only while `enabled()` is true (`idp_outbox_enabled`,
  default off). With the switch off, the caller does no other work for it.
- `recipients` holds slack_user_id values. The read route changes them into
  work emails. A pending event goes to the admins that the Slack fan-out DMs,
  from the same list. A decided event goes to the requester.
- Every payload value is a string (Ruling P-10). The panel decodes the payload
  into Go's map[string]string. One number or null stops its whole poll.
- This module logs a failure and swallows it. The request is already
  saved or decided, and a lost outbox row must never fail it.

`request_id` stays NULL. That column holds the id of a SQL request
(`requests.id`), and an access request or a window request has its own id
sequence. The payload carries that id as `requestId`. The panel's poller
reads the id from the payload only.

Slack does not decide whether a caller writes a row. These rows are for the
time when Slack is off, so every writer works without a Slack client.
"""
from __future__ import annotations

import json
import logging

from . import config as cfg
from . import db, window_options

log = logging.getLogger(__name__)

ACCESS_REQUEST_PENDING = "queryhub.access_request_pending"
ACCESS_REQUEST_DECIDED = "queryhub.access_request_decided"
WINDOW_REQUEST_PENDING = "queryhub.window_request_pending"
WINDOW_REQUEST_DECIDED = "queryhub.window_request_decided"

# The web route writes this into `attempted_query` for a server that QueryHub
# does not have yet: web-endpoint-request:<server>|<TIER>|<database>.
# See web/routes_requests.endpoint_request.
_FREE_TEXT_MARK = "web-endpoint-request:"


def enabled() -> bool:
    """True while `idp_outbox_enabled` is on (default off).

    A failed read counts as off. Then the caller writes no row and its
    request continues."""
    try:
        return cfg.get_bool("idp_outbox_enabled", False)
    except Exception:
        log.exception("could not read idp_outbox_enabled, so QueryHub writes no "
                      "notification_outbox row for this event.")
        return False


def _text(value) -> str:
    """A payload value as the panel needs it: a string, and "" for none."""
    return "" if value is None else str(value)


def _insert(event_type: str, recipients: list[str], payload: dict) -> None:
    """Insert one row. A row with no recipients tells nobody, so this function
    skips it. The caller catches every error."""
    recipients = [r for r in recipients if r]
    if not recipients:
        return
    db.execute(
        "INSERT INTO notification_outbox "
        "(event_type, request_id, recipients, payload) "
        "VALUES (%s, NULL, %s, %s)",
        (event_type, recipients,
         json.dumps({key: _text(value) for key, value in payload.items()})),
    )


def _lost(event_type: str, request_id) -> None:
    log.exception(
        "failed to write the %s notification_outbox row for request %s. The "
        "request is saved and Slack is unchanged, but the IdP panel does not "
        "get this event.", event_type, request_id)


REQUEST_DECIDED = "queryhub.request_decided"
REQUEST_WITHDRAWN = "queryhub.request_withdrawn"
REQUEST_EXPIRED = "queryhub.request_expired"

# The decision word of `queryhub.request_decided`, by `core_decide` decision.
_DECISION_WORDS = {"approve": "approved", "reject": "rejected",
                   "changes": "changes_requested"}


def _insert_for_request(event_type: str, request_id, requester: str | None,
                        payload: dict) -> None:
    """Insert one row of a SQL request event. `request_id` is `requests.id`,
    like `queryhub.request_pending`. The requester is the only recipient.
    The caller catches every error."""
    if not requester:
        return
    db.execute(
        "INSERT INTO notification_outbox "
        "(event_type, request_id, recipients, payload) "
        "VALUES (%s, %s, %s, %s)",
        (event_type, request_id, [requester],
         json.dumps({key: _text(value) for key, value in payload.items()})),
    )


def _alias_of(target_id) -> str:
    from . import targets
    target = targets.get(target_id) if target_id else None
    return target.alias if target is not None else ""


def write_request_decided(row: dict, decision: str) -> None:
    """An admin approved, rejected or asked for changes on a SQL request.
    `row` is the decided row that `core_decide` returns, after its transaction
    commits. `decision` is the `core_decide` word: approve, reject or changes.
    The payload never holds the SQL text or a result value."""
    if not enabled():
        return
    try:
        _insert_for_request(
            REQUEST_DECIDED, row["id"], row.get("requester_slack_id"),
            {"requestId": row["id"],
             "decision": _DECISION_WORDS[decision],
             "target": _alias_of(row.get("target_server_id")),
             "tier": row.get("required_tier"),
             "decidedBy": row.get("decided_by_name") or row.get("decided_by_slack_id"),
             "note": row.get("decision_reason")})
    except Exception:
        _lost(REQUEST_DECIDED, row.get("id"))


def write_request_withdrawn(request_id: int, by_name: str | None) -> None:
    """The requester withdrew a SQL request. Call it after the row is
    withdrawn and the transaction commits."""
    if not enabled():
        return
    try:
        row = db.fetch_one(
            "SELECT requester_slack_id, target_server_id, required_tier "
            "FROM requests WHERE id = %s", (request_id,))
        if not row:
            return
        _insert_for_request(
            REQUEST_WITHDRAWN, request_id, row.get("requester_slack_id"),
            {"requestId": request_id,
             "target": _alias_of(row.get("target_server_id")),
             "tier": row.get("required_tier"),
             "decidedBy": by_name,
             "note": ""})
    except Exception:
        _lost(REQUEST_WITHDRAWN, request_id)


def write_request_expired(request_id: int, requester: str | None, alias: str | None,
                          tier: str | None, note: str = "") -> None:
    """The stale-pending job expired a SQL request. Call it after the row is
    expired and the transaction commits."""
    if not enabled():
        return
    try:
        _insert_for_request(
            REQUEST_EXPIRED, request_id, requester,
            {"requestId": request_id, "target": alias, "tier": tier,
             "decidedBy": "", "note": note})
    except Exception:
        _lost(REQUEST_EXPIRED, request_id)


def _access_target(row: dict, target) -> str:
    """The alias of the target. For a server that QueryHub does not have yet,
    the name that the requester typed. "" when QueryHub knows neither.

    The web route keeps the typed name only in `attempted_query`, between the
    mark and the tier and database that the row also stores. So the name is
    the part between those two, never a guess."""
    if target is not None:
        return target.alias
    query = row.get("attempted_query") or ""
    if not query.startswith(_FREE_TEXT_MARK):
        return ""
    tail = f"|{(row.get('requested_tier') or '').upper()}|{row.get('database_name') or ''}"
    named = query[len(_FREE_TEXT_MARK):]
    return named[:-len(tail)] if named.endswith(tail) else ""


def _window_label(minutes) -> str:
    return window_options.window_label(minutes) if minutes else ""


def access_request_pending(row: dict, target, admins_told: list[dict]) -> None:
    """A new access request. `admins_told` is the list that the Slack fan-out
    DMs (`access_requests.notify_admins`)."""
    try:
        from .access_requests import requested_tier_of
        _insert(ACCESS_REQUEST_PENDING,
                [a["slack_user_id"] for a in admins_told],
                {"requestId": row["id"],
                 "requesterName": row.get("requester_name") or row.get("requester_slack_id"),
                 "target": _access_target(row, target),
                 "database": row.get("database_name"),
                 "tier": requested_tier_of(row),
                 "reason": row.get("reason")})
    except Exception:
        _lost(ACCESS_REQUEST_PENDING, row.get("id"))


def access_request_decided(row: dict) -> None:
    """An admin approved or rejected an access request. `row` is what
    `access_requests.decide` returns, after its transaction commits.

    `grantApplied` is "true" only when the approval wrote the grant. An
    approval can skip the grant (a tier conflict, or no target yet). Then the
    requester has no access yet, and the Slack DM says so too."""
    try:
        from . import targets
        from .access_requests import requested_tier_of
        tid = row.get("target_server_id")
        target = targets.get(tid) if tid else None
        _insert(ACCESS_REQUEST_DECIDED,
                [row["requester_slack_id"]],
                {"requestId": row["id"],
                 "decision": row.get("status"),
                 "target": _access_target(row, target),
                 "database": row.get("database_name"),
                 "tier": requested_tier_of(row),
                 "deciderName": row.get("decided_by_name") or row.get("decided_by_slack_id"),
                 "note": row.get("decision_reason"),
                 "grantApplied": "true" if (row.get("auto_grant") or {}).get("applied")
                 else "false"})
    except Exception:
        _lost(ACCESS_REQUEST_DECIDED, row.get("id"))


def window_request_pending(row: dict, alias: str, admins_told: list[dict]) -> None:
    """A new auto-approve window request. `admins_told` is the list that the
    Slack fan-out DMs (`auto_approve_requests.notify_admins`)."""
    try:
        _insert(WINDOW_REQUEST_PENDING,
                [a["slack_user_id"] for a in admins_told],
                {"requestId": row["id"],
                 "requesterName": row.get("requester_name") or row.get("requester_slack_id"),
                 "target": alias,
                 "database": row.get("database_name"),
                 "tier": row.get("max_tier") or "ro",
                 "reason": row.get("reason"),
                 "windowLabel": _window_label(row.get("window_minutes"))})
    except Exception:
        _lost(WINDOW_REQUEST_PENDING, row.get("id"))


def window_request_decided(request: dict, *, approved: bool, alias: str,
                           decider_name: str | None) -> None:
    """An admin approved or rejected a window request. `request` is the row that
    `auto_approve_requests.decide_window` read, after the decision commits.
    A window decision has no note, so `note` is always ""."""
    try:
        _insert(WINDOW_REQUEST_DECIDED,
                [request["requester_slack_id"]],
                {"requestId": request["id"],
                 "decision": "approved" if approved else "rejected",
                 "target": alias,
                 "database": request.get("database_name"),
                 "tier": request.get("max_tier") or "ro",
                 "deciderName": decider_name,
                 "note": "",
                 "windowLabel": _window_label(request.get("window_minutes"))})
    except Exception:
        _lost(WINDOW_REQUEST_DECIDED, request.get("id"))
