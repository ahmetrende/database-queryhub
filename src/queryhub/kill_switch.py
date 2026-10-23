"""The kill switch: the reads it stops, and the history of the pauses.

The switch itself is `bot_config.kill_switch`, which `core_submit` reads. Two
doors set it: `POST /api/admin/kill` and `/sql kill`. Each writes the switch
and one `kill_switch_set` audit row in one transaction. When the switch goes
on, the door then calls `stop_running_reads`, which cancels the running reads
and writes one `kill_switch_cancelled` row with the count. This module also
reads those rows back as a list of pauses (`GET /api/admin/kill/history`).
"""
from __future__ import annotations

import json
import logging

from . import db

log = logging.getLogger(__name__)

SET_ACTION = "kill_switch_set"
CANCELLED_ACTION = "kill_switch_cancelled"

# The newest audit rows that the history reads. A pause whose ON row is older
# than these is not listed. The switch is set a few times a year at most.
HISTORY_ROWS = 1000


def stop_running_reads(actor_id: str, actor_name: str | None) -> int | None:
    """Cancel the running reads of every user, after the switch went on, and
    record how many stopped. Returns that number, or None when it is not known.

    The switch is on before this runs, so no new query starts while it works.
    The count goes into a `kill_switch_cancelled` row of its own, because the
    row of the pause is committed already and the audit log is append-only.
    Nothing here raises: a failed sweep is a count of None, and a failed write
    of the row is logged. The caller sets the switch whatever happens here."""
    from . import audit, cancellation
    try:
        cancelled = cancellation.cancel_running_reads(actor_id, actor_name)
    except Exception:
        log.exception("kill switch: the running reads were not cancelled")
        cancelled = None
    try:
        with db.transaction() as cur:
            audit.log_in(cur, None, actor_id, actor_name, CANCELLED_ACTION,
                         {"cancelledCount": cancelled})
    except Exception:
        log.exception("kill switch: could not record the count of cancelled reads")
    return cancelled


def _details(row: dict) -> dict:
    d = row.get("details")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            return {}
    return d if isinstance(d, dict) else {}


def _enabled(d: dict) -> str:
    """'true', 'false' or something else, read as `details->>'enabled'` reads
    it. GET /api/admin/kill asks the same question in SQL."""
    v = d.get("enabled")
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).strip().lower()


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _count(value) -> int | None:
    """An int, or None. null, never 0, for a value that does not say: 0 would
    claim that nothing ran, and nobody counted."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def periods(rows: list[dict]) -> list[dict]:
    """Pair the audit rows of the switch into pauses. Newest pause first.

    `rows` are `kill_switch_set` and `kill_switch_cancelled` rows in id order,
    oldest first. A set row with `enabled = true` starts a pause. The next set
    row with `enabled = false` ends it. A pause with no such row is still on,
    so its `endedAt` and `endedBy` are None. Somebody can set the switch on
    while it is on already, for example to change the message. That row starts
    one more pause, and the next off row ends both. An off row with no open
    pause ends nothing.

    A cancelled row gives its count to the newest pause of the same person
    that has no count yet. In the normal order, that is the newest open pause:
    the switch goes on, then the reads stop. The same-person rule also keeps
    the count when somebody sets the switch off before the cancels end. It
    also keeps apart two people who set the switch on at once. A pause with no
    cancelled row keeps `cancelledCount` null, never 0.
    """
    out: list[dict] = []
    still_on: list[dict] = []
    uncounted: list[tuple[str | None, dict]] = []     # (person, pause), oldest first
    for row in rows:
        d = _details(row)
        who = row.get("actor_name") or row.get("actor_slack_id")
        if row.get("action") == CANCELLED_ACTION:
            mine = [i for i, (person, _p) in enumerate(uncounted)
                    if person == row.get("actor_slack_id")]
            if mine:
                _person, pause = uncounted.pop(mine[-1])
                pause["cancelledCount"] = _count(d.get("cancelledCount"))
            continue
        state = _enabled(d)
        if state == "true":
            pause = {
                "startedAt": _iso(row.get("created_at")),
                "by": who,
                "message": d.get("message"),
                "endedAt": None,
                "endedBy": None,
                "cancelledCount": None,
            }
            out.append(pause)
            still_on.append(pause)
            uncounted.append((row.get("actor_slack_id"), pause))
        elif state == "false":
            for pause in still_on:
                pause["endedAt"] = _iso(row.get("created_at"))
                pause["endedBy"] = who
            still_on = []
    out.reverse()
    return out


def history(limit: int = HISTORY_ROWS) -> list[dict]:
    """Every pause in the newest `limit` rows of the switch, newest first."""
    rows = db.fetch_all(
        "SELECT id, action, actor_slack_id, actor_name, details, created_at "
        "  FROM audit_log WHERE action IN (%s, %s) "
        " ORDER BY id DESC LIMIT %s", (SET_ACTION, CANCELLED_ACTION, limit))
    return periods(list(reversed(rows)))
