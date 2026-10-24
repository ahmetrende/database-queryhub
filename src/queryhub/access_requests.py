"""Access-request CRUD: when a user can't /sql against a target/database,
they can submit one of these for admin review. Schema in migration 005.

Approving one AUTO-CREATES the per-user grant from the request's own fields
(requester + target + database + requested tier) in the same transaction —
the admin no longer has to hand-run the grant SQL for the common case. The
auto-grant is deliberately least-privilege and conservative:

  - no target on the request (free-text server)  -> nothing to grant; the
    admin onboards the target first (status still flips to approved).
  - tier defaults to 'ro' when the request didn't state one.
  - an existing ACTIVE grant with a DIFFERENT tier is never touched (neither
    upgraded nor downgraded) — the auto-grant is skipped and flagged so the
    admin applies the change deliberately. Same-tier grants merge databases.
  - a revoked grant, or a grant past its end date, is dead. The approval
    starts a fresh grant. The fresh grant ends in `access_grant_default_days`
    days (default 90), unless the approver picked an end or picked no end
    (`access_grant_default.approval_end`). An ACTIVE same-tier grant keeps its
    end date: an approval never lengthens or removes a limit that an admin
    set.

The requester can withdraw a pending request (`withdraw`, migration 160). A
withdrawn request is closed, and no admin decided it.

`notify_admins` tells the admins about a new request. It imports `slack_app`
only when Slack is on and a client exists. So the web route works when Slack
is off, and also when the `slack_app` package is not there.
"""
from __future__ import annotations

import psycopg

from . import access_grant_default, audit, db, idp_outbox
from . import config as cfg

_TIER_RANK = {"ro": 0, "rw": 1, "ddl": 2}


class ControlPlaneRefused(ValueError):
    """Raised when a request or grant names the bot's own metadata database."""


def _refuse_control_plane(target_server_id: int | None) -> None:
    """The bot's own control-plane database is never grantable through this
    flow.

    `grants.grant` and `grants.grant_many` have refused it since the DDL-only
    elevation work, and the web endpoint-request route refuses it at
    submission (`routes_requests.py`, "That connection cannot be requested").
    This path had neither check and does not call `grants.grant` at all -- it
    writes `user_target_grants` directly -- so the Slack access-request flow
    could hand out a grant that every other path refuses. The Slack target
    picker offers it too: `handle_access_target_options` deliberately returns
    ALL enabled targets, because a person asking for access is by definition
    asking for something they cannot reach.

    A grant here is not an ordinary over-grant. `queryhub` holds the audit
    log, the grant tables and the config that decides who may approve, so read
    access is a disclosure of the whole access model and write access would let
    somebody edit the record of their own actions.
    """
    if target_server_id is None:
        return
    from . import grants          # lazy: grants imports this module's siblings
    if int(target_server_id) in grants.control_plane_target_ids():
        raise ControlPlaneRefused(
            "That connection is the bot's own control-plane database and "
            "cannot be requested or granted here.")


def requested_tier_of(row: dict) -> str:
    """The tier an access request asks for, from the persisted `requested_tier`
    column only, defaulting to least-privilege 'ro'.

    SECURITY: this must NOT fall back to parsing the free-text `reason`. The
    web flow prepends a "[requested tier: X]" label to `reason` for display,
    but the Slack flow's `reason` is raw user input — trusting a prefix there
    would let a requester self-select the grant tier (write "[requested tier:
    ddl]" and get DDL on approve). Only the server-set column is trusted;
    migration 076 backfilled it once from the (server-set) web prefix."""
    tier = (row.get("requested_tier") or "").strip().lower()
    return tier if tier in _TIER_RANK else "ro"


def _merge_databases(existing: list | None, new: list | None) -> list | None:
    """Union of two allowed_databases values, where None means ALL databases
    (so None absorbs everything)."""
    if existing is None or new is None:
        return None
    return sorted(set(existing) | set(new))


def find_pending_for(
    principal_id: str,
    target_server_id: int | None,
    attempted_query: str | None,
) -> dict | None:
    """Return the existing pending request that would clash with a new one
    from this (user, target, query) — or None. Used so we can show a friendly
    "you already have a pending request" message instead of a unique-violation
    error."""
    return db.fetch_one(
        "SELECT id, created_at FROM access_requests "
        "WHERE status = 'pending' "
        "  AND requester_slack_id = %s "
        "  AND COALESCE(target_server_id, 0) = COALESCE(%s, 0) "
        "  AND md5(COALESCE(attempted_query, '')) = md5(COALESCE(%s, '')) "
        "LIMIT 1",
        (principal_id, target_server_id, attempted_query),
    )


def open_count_for(principal_id: str) -> int:
    """How many pending access requests this user has. Used to rate-limit
    the web endpoint-request flow (each one DMs every admin)."""
    row = db.fetch_one(
        "SELECT count(*) AS n FROM access_requests "
        "WHERE requester_slack_id = %s AND status = 'pending'",
        (principal_id,),
    )
    return int(row["n"]) if row else 0


def create(
    principal_id: str,
    name: str | None,
    target_server_id: int | None,
    database_name: str | None,
    attempted_query: str | None,
    reason: str,
    requested_tier: str | None = None,
) -> dict | None:
    """Insert a new pending access request. Returns the new row, or None if
    a pending duplicate already exists (the unique partial index will reject
    on conflict — we let psycopg raise UniqueViolation and translate to None)."""
    tier = (requested_tier or "").strip().lower() or None
    if tier is not None and tier not in _TIER_RANK:
        raise ValueError(f"invalid requested_tier: {requested_tier!r}")
    _refuse_control_plane(target_server_id)
    try:
        return db.insert_returning(
            "INSERT INTO access_requests "
            "(requester_slack_id, requester_name, target_server_id, "
            " database_name, attempted_query, reason, requested_tier) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id, requester_slack_id, requester_name, "
            "          target_server_id, database_name, attempted_query, "
            "          reason, requested_tier, status, created_at",
            (
                principal_id,
                name,
                target_server_id,
                database_name,
                attempted_query,
                reason,
                tier,
            ),
        )
    except psycopg.errors.UniqueViolation:
        return None


_ROW_COLS = ("id, requester_slack_id, requester_name, target_server_id, "
             "database_name, attempted_query, reason, requested_tier, status, "
             "decided_by_slack_id, decided_by_name, decision_reason, "
             "created_at, decided_at")


def get(access_request_id: int) -> dict | None:
    return db.fetch_one(
        f"SELECT {_ROW_COLS} FROM access_requests WHERE id = %s",
        (access_request_id,),
    )


def list_for(principal_id: str, limit: int = 200) -> list[dict]:
    """The access requests that one person filed, newest first.

    The WHERE names the person, so a caller gets only its own rows."""
    return db.fetch_all(
        f"SELECT {_ROW_COLS} FROM access_requests "
        "WHERE requester_slack_id = %s ORDER BY id DESC LIMIT %s",
        (principal_id, limit),
    )


def withdraw(access_request_id: int, by_id: str,
             by_name: str | None) -> dict | None:
    """The requester withdraws their own pending request. Returns the updated
    row, or None when nothing changed.

    Nothing changes when the request is not theirs or is not pending any more:
    an admin decided it, or the requester withdrew it already. The WHERE names
    the requester, so this function cannot withdraw the request of another
    person. The status test makes a race with an admin's decision safe. The
    first UPDATE wins, and the other one changes nothing. An approval that
    loses writes no grant, because `decide()` writes the grant in the
    transaction of its own UPDATE.

    The status change and its audit row commit in one transaction. The row
    keeps no decider and no note: no admin decided it. `decided_at` is the
    time of the withdrawal, which closes the request.

    A database function only. The caller retires the admins' Slack cards,
    as the web withdraw of a SQL request does."""
    with db.transaction() as cur:
        cur.execute(
            "UPDATE access_requests SET status = 'withdrawn', decided_at = NOW() "
            "WHERE id = %s AND requester_slack_id = %s AND status = 'pending' "
            f"RETURNING {_ROW_COLS}",
            (access_request_id, by_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        row = dict(row)
        audit.log_in(cur, None, by_id, by_name, "access_request_withdrawn",
                     {"access_request_id": row["id"],
                      "target_server_id": row["target_server_id"],
                      "database": row["database_name"],
                      "tier": requested_tier_of(row)})
    return row


def _auto_grant(cur, row: dict, decided_by_slack_id: str,
                decided_by_name: str | None, *,
                end=None, end_source: str = "default") -> dict:
    """Create/extend the per-user grant an approved request asks for, inside
    the caller's transaction. Returns a summary dict:

        {"applied": bool, "reason": str, "mode": ..., "databases": ...,
         "expires_at": datetime | None}     # "expires_at" only when applied

    Conservative by design (see module docstring). An unknown target is
    skipped. An active grant at a different tier is skipped: the code never
    upgrades or downgrades one silently. A same-tier grant gets the databases
    merged (None = all absorbs) and keeps its end date. A revoked or lapsed
    grant is replaced by a fresh one that ends at `end`. `end` is the answer of
    `access_grant_default.approval_end` (None is no end), and `end_source` says
    where it came from for the audit row: default, chosen or no_end."""
    # Refused here as a REASON, not an exception. The approve button already
    # renders "auto-grant skipped, and why", and a request created before this
    # guard existed must stay decidable -- an admin has to be able to reject
    # it. `create()` raises instead, because there the person is still holding
    # the form and can be told.
    if row.get("target_server_id") is not None:
        from . import grants
        if int(row["target_server_id"]) in grants.control_plane_target_ids():
            return {"applied": False, "reason": "control_plane",
                    "mode": None, "databases": None}

    tid = row.get("target_server_id")
    if tid is None:
        return {"applied": False, "reason": "no_target",
                "mode": None, "databases": None}

    uid = row["requester_slack_id"]
    tier = requested_tier_of(row)
    new_dbs = [row["database_name"]] if row.get("database_name") else None

    # Whitelist safety net: a grant is useless without an enabled requesters
    # row. Requesters are normally already whitelisted (both surfaces gate on
    # it), so this is a no-op upsert that never downgrades existing fields.
    cur.execute(
        "INSERT INTO requesters (slack_user_id, name, enabled, added_by) "
        "VALUES (%s, %s, TRUE, %s) "
        "ON CONFLICT (slack_user_id) DO UPDATE "
        "  SET enabled = TRUE, "
        "      name = COALESCE(requesters.name, EXCLUDED.name)",
        (uid, row.get("requester_name"), decided_by_slack_id),
    )

    # `cur` may be a psycopg Connection or Cursor — .execute() returns a
    # cursor either way, so capture it for the fetch.
    # The database decides "active", on its own clock, with the test the grant
    # readers use. A grant is active when it is not revoked and has no end date
    # or one still ahead. A grant past its end date is NOT active, even though
    # `revoked_at` is NULL.
    res = cur.execute(
        "SELECT mode, allowed_databases, expires_at, "
        "       (revoked_at IS NULL "
        "        AND (expires_at IS NULL OR expires_at > NOW())) AS active "
        "FROM user_target_grants "
        "WHERE slack_user_id = %s AND target_server_id = %s FOR UPDATE",
        (uid, tid),
    )
    existing = res.fetchone()

    if existing is not None and existing["active"]:
        if existing["mode"] != tier:
            # An active grant at another tier: changing it (either way) is a
            # security decision, not bookkeeping — leave it to the admin.
            return {"applied": False, "reason": "tier_conflict",
                    "mode": existing["mode"],
                    "databases": existing["allowed_databases"]}
        dbs = _merge_databases(existing["allowed_databases"], new_dbs)
        # The limit that is already there stays. An approval must not lengthen
        # it, and it must not remove it.
        expires_at = existing["expires_at"]
        end_source = "kept"
    else:
        # There is no grant, or it is revoked, or it has lapsed. This approval
        # starts a fresh grant. The old end date must not carry over. The
        # upsert below used to leave `expires_at` alone. A person who asked
        # again after a lapse got an approval and a "your access is active"
        # message, and the system still refused them. The fresh grant gets the
        # end that the approval chose, or the default.
        dbs = new_dbs
        expires_at = end

    cur.execute(
        "INSERT INTO user_target_grants "
        "  (slack_user_id, target_server_id, allowed_databases, mode, "
        "   granted_by, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (slack_user_id, target_server_id) DO UPDATE "
        "   SET allowed_databases = EXCLUDED.allowed_databases, "
        "       mode              = EXCLUDED.mode, "
        "       granted_by        = EXCLUDED.granted_by, "
        "       granted_at        = NOW(), "
        "       revoked_at        = NULL, "
        "       expires_at        = EXCLUDED.expires_at",
        (uid, tid, dbs, tier, decided_by_slack_id, expires_at),
    )
    audit.log_in(cur, None, decided_by_slack_id, decided_by_name,
                 "access_request_auto_grant",
                 {"access_request_id": row["id"], "grantee": uid,
                  "target_server_id": tid, "mode": tier, "databases": dbs,
                  "expires_at": expires_at.isoformat() if expires_at else None,
                  "end_source": end_source})
    return {"applied": True, "reason": "granted", "mode": tier,
            "databases": dbs, "expires_at": expires_at}


def decide(
    access_request_id: int,
    new_status: str,
    decided_by_slack_id: str,
    decided_by_name: str | None,
    decision_reason: str | None,
    *,
    expires_at=None,
    no_end: bool = False,
) -> dict | None:
    """Mark a pending request as approved/rejected. Returns the updated row,
    or None if it was already decided (so a second admin clicking is a no-op).

    On APPROVE the new grant ends at `expires_at` (a datetime or an ISO-8601
    string), has no end when `no_end` is true, and otherwise ends in
    `access_grant_default_days` days. The Slack button sends neither, so it
    takes the default. A bad end raises `access_grant_default.EndRefused`
    before anything is written. `expires_at` and `no_end` mean nothing on a
    reject.

    On APPROVE the per-user grant is auto-created from the request in the
    same transaction (see _auto_grant); the returned row carries the summary
    under "auto_grant". The auth-event outbox trigger on user_target_grants
    DMs the requester about the new access on its own.

    The Slack buttons and the web screen both decide through here. So this
    is the one place that writes the IdP panel's
    `queryhub.access_request_decided` row, after the commit."""
    if new_status not in ("approved", "rejected"):
        raise ValueError(f"invalid status: {new_status}")
    end, end_source = None, "default"
    if new_status == "approved":
        # Checked before the transaction, so a bad end writes nothing.
        end = access_grant_default.approval_end(expires_at, no_end=no_end)
        end_source = ("no_end" if no_end
                      else "chosen" if expires_at not in (None, "") else "default")
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE access_requests SET status = %s, "
            "       decided_by_slack_id = %s, decided_by_name = %s, "
            "       decision_reason = %s, decided_at = NOW() "
            "WHERE id = %s AND status = 'pending' "
            "RETURNING id, requester_slack_id, requester_name, target_server_id, "
            "          database_name, attempted_query, reason, requested_tier, "
            "          status, decided_by_slack_id, decided_by_name, "
            "          decision_reason, created_at, decided_at",
            (new_status, decided_by_slack_id, decided_by_name,
             decision_reason, access_request_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        row = dict(row)
        if new_status == "approved":
            row["auto_grant"] = _auto_grant(conn, row, decided_by_slack_id,
                                            decided_by_name, end=end,
                                            end_source=end_source)
    # Outside the transaction: the decision commits first, and a lost outbox
    # row can never undo it.
    if idp_outbox.enabled():
        idp_outbox.access_request_decided(row)
    return row


def notify_admins(client, new_row: dict, target,
                  requested_server: str | None = None) -> str | None:
    """Tell every active admin about a new access request, and return the ts
    of the first DM that Slack delivered. Return None when no DM went out: no
    active admins, or no Slack transport.

    The web "Request new endpoint" route calls this function. The Slack
    blocked-query flow calls it through `slack_app.access.fan_out_admin_dms`.

    While `idp_outbox_enabled` is on, it writes the IdP panel's
    `queryhub.access_request_pending` row. That row does not need Slack, and it
    names the admins that the DMs go to. `slack_app.access.dm_admins` sends the
    DMs. This function imports `slack_app` only when Slack is on and a client
    exists.

    Callers must read None as "not notified", never as "not saved"."""
    from . import admins

    slack_on = bool(cfg.ENV.slack_enabled and client is not None)
    outbox_on = idp_outbox.enabled()
    # Vanilla (no-Slack) profile: there is no client to send with, so stop
    # before the DM loop. This guard was missing once. `client` is None in the
    # vanilla profile, so `conversations_open` raised AttributeError. The
    # per-admin `except Exception` hid it once per admin, and the function
    # returned None. The web caller then answered 503 for a request that it
    # saved. The senders in `slack_app/notifications.py` stop the same way.
    if not slack_on and not outbox_on:
        return None

    # One list for the DMs and for the outbox row, so that both name the same
    # admins. The row goes first: it must not depend on Slack delivery.
    admins_told = admins.list_active()
    if outbox_on:
        idp_outbox.access_request_pending(new_row, target, admins_told)
    if not slack_on:
        return None
    from .slack_app import access
    return access.dm_admins(client, new_row, target, admins_told,
                            requested_server=requested_server)


def record_admin_dm(
    access_request_id: int,
    admin_slack_id: str,
    channel_id: str,
    message_ts: str,
) -> None:
    db.execute(
        "INSERT INTO access_request_notifications "
        "(access_request_id, admin_slack_id, channel_id, message_ts) "
        "VALUES (%s, %s, %s, %s)",
        (access_request_id, admin_slack_id, channel_id, message_ts),
    )


def list_admin_dms(access_request_id: int) -> list[dict]:
    return db.fetch_all(
        "SELECT channel_id, message_ts FROM access_request_notifications "
        "WHERE access_request_id = %s",
        (access_request_id,),
    )
