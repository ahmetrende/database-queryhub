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
"""
from __future__ import annotations

import psycopg

from . import audit, db

_TIER_RANK = {"ro": 0, "rw": 1, "ddl": 2}


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


def get(access_request_id: int) -> dict | None:
    return db.fetch_one(
        "SELECT id, requester_slack_id, requester_name, target_server_id, "
        "       database_name, attempted_query, reason, requested_tier, status, "
        "       decided_by_slack_id, decided_by_name, decision_reason, "
        "       created_at, decided_at "
        "FROM access_requests WHERE id = %s",
        (access_request_id,),
    )


def _auto_grant(cur, row: dict, decided_by_slack_id: str,
                decided_by_name: str | None) -> dict:
    """Create/extend the per-user grant an approved request asks for, inside
    the caller's transaction. Returns a summary dict:

        {"applied": bool, "reason": str, "mode": ..., "databases": ...}

    Conservative by design (see module docstring): unknown target -> skip;
    active grant at a different tier -> skip (never silently upgrade or
    downgrade); same tier -> merge databases (None = all absorbs)."""
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
    res = cur.execute(
        "SELECT mode, allowed_databases, revoked_at FROM user_target_grants "
        "WHERE slack_user_id = %s AND target_server_id = %s FOR UPDATE",
        (uid, tid),
    )
    existing = res.fetchone()

    if existing is not None and existing["revoked_at"] is None:
        if existing["mode"] != tier:
            # An active grant at another tier: changing it (either way) is a
            # security decision, not bookkeeping — leave it to the admin.
            return {"applied": False, "reason": "tier_conflict",
                    "mode": existing["mode"],
                    "databases": existing["allowed_databases"]}
        dbs = _merge_databases(existing["allowed_databases"], new_dbs)
    else:
        dbs = new_dbs

    cur.execute(
        "INSERT INTO user_target_grants "
        "  (slack_user_id, target_server_id, allowed_databases, mode, granted_by) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (slack_user_id, target_server_id) DO UPDATE "
        "   SET allowed_databases = EXCLUDED.allowed_databases, "
        "       mode              = EXCLUDED.mode, "
        "       granted_by        = EXCLUDED.granted_by, "
        "       granted_at        = NOW(), "
        "       revoked_at        = NULL",
        (uid, tid, dbs, tier, decided_by_slack_id),
    )
    audit.log_in(cur, None, decided_by_slack_id, decided_by_name,
                 "access_request_auto_grant",
                 {"access_request_id": row["id"], "grantee": uid,
                  "target_server_id": tid, "mode": tier, "databases": dbs})
    return {"applied": True, "reason": "granted", "mode": tier, "databases": dbs}


def decide(
    access_request_id: int,
    new_status: str,
    decided_by_slack_id: str,
    decided_by_name: str | None,
    decision_reason: str | None,
) -> dict | None:
    """Mark a pending request as approved/rejected. Returns the updated row,
    or None if it was already decided (so a second admin clicking is a no-op).

    On APPROVE the per-user grant is auto-created from the request in the
    same transaction (see _auto_grant); the returned row carries the summary
    under "auto_grant". The auth-event outbox trigger on user_target_grants
    DMs the requester about the new access on its own."""
    if new_status not in ("approved", "rejected"):
        raise ValueError(f"invalid status: {new_status}")
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
                                            decided_by_name)
        return row


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
