"""CRUD helpers for per-user starred queries ("favorites").

Schema: `migrations/049_query_favorites.sql`. Favorites are a lighter,
personal-only sibling of query_templates: no name, no sharing. A user
stars a query they ran (the result-DM ⭐ button or the /sql modal's
"favorite this" checkbox) and it shows up in a personal picker in the
modal, mirroring the load-from-template flow.

Deduped per (user, query, target, database) by a unique index, so
re-starring the same query just bumps last_used_at.
"""
from __future__ import annotations

from . import db

# Keep a user's favorites list bounded so the picker stays usable and the
# table doesn't grow without limit. When a new favorite pushes past this,
# the least-recently-used ones are trimmed.
MAX_PER_USER = 50

# Label shown in the picker when the user didn't give one — a single-line
# preview of the query, collapsed whitespace, capped.
_PREVIEW_LEN = 60


def preview(query: str) -> str:
    """One-line, whitespace-collapsed snippet of a query for picker labels."""
    return " ".join((query or "").split())[:_PREVIEW_LEN]


# ---------- writes ------------------------------------------------------


def add(*,
        principal_id: str,
        query: str,
        target_server_id: int | None,
        database_name: str | None,
        label: str | None = None) -> dict:
    """Star a query for one user. Dedup index collapses a repeat star into
    a last_used_at touch (+ use_count bump). Returns the resulting row.
    Trims the user back to MAX_PER_USER (least-recently-used dropped)."""
    row = db.insert_returning(
        """
        INSERT INTO query_favorites
            (slack_user_id, query, target_server_id, database_name, label,
             last_used_at, use_count)
        VALUES (%s, %s, %s, %s, %s, NOW(), 1)
        ON CONFLICT (slack_user_id, md5(query),
                     COALESCE(target_server_id, -1), COALESCE(database_name, ''))
        DO UPDATE SET last_used_at = NOW(),
                      use_count    = query_favorites.use_count + 1,
                      label        = COALESCE(EXCLUDED.label, query_favorites.label)
        RETURNING id, slack_user_id, query, target_server_id, database_name,
                  label, created_at, last_used_at, use_count
        """,
        (principal_id, query, target_server_id, database_name, label),
    )
    _trim(principal_id)
    return row


def _trim(principal_id: str) -> None:
    """Drop the user's favorites beyond MAX_PER_USER, least-recently-used
    first (a favorite never used yet sorts by created_at via NULLS LAST)."""
    db.execute(
        """
        DELETE FROM query_favorites
         WHERE slack_user_id = %s
           AND id NOT IN (
               SELECT id FROM query_favorites
                WHERE slack_user_id = %s
                ORDER BY last_used_at DESC NULLS LAST, created_at DESC
                LIMIT %s
           )
        """,
        (principal_id, principal_id, MAX_PER_USER),
    )


def record_use(favorite_id: int) -> None:
    """Bump use_count + last_used_at when a favorite is loaded into the modal."""
    db.execute(
        "UPDATE query_favorites "
        "   SET use_count = use_count + 1, last_used_at = NOW() "
        " WHERE id = %s",
        (favorite_id,),
    )


def delete(principal_id: str, favorite_id: int) -> bool:
    """Remove the user's own favorite. True if a row was deleted."""
    row = db.fetch_one(
        "DELETE FROM query_favorites "
        " WHERE id = %s AND slack_user_id = %s RETURNING id",
        (favorite_id, principal_id),
    )
    return row is not None


# ---------- reads -------------------------------------------------------


def get(favorite_id: int) -> dict | None:
    """Fetch by id. Used by the modal load-favorite action to resolve the
    picked option. Caller must still check ownership before loading."""
    return db.fetch_one(
        "SELECT id, slack_user_id, query, target_server_id, database_name, "
        "       label, created_at, last_used_at, use_count "
        "  FROM query_favorites WHERE id = %s",
        (favorite_id,),
    )


def search_for_picker(principal_id: str, typed: str = "",
                      limit: int = 100) -> list[dict]:
    """Flat list for the modal's favorites external_select. The user's own
    favorites, most-recently-used first, filtered by a case-insensitive
    substring match on label-or-query when `typed` is given.

    Each row: {id, query, target_alias, database_name, label}."""
    typed = (typed or "").strip()
    return db.fetch_all(
        """
        SELECT f.id, f.query, f.database_name, f.label,
               (SELECT alias FROM target_servers WHERE id = f.target_server_id)
                   AS target_alias
          FROM query_favorites f
         WHERE f.slack_user_id = %s
           AND (%s = '' OR lower(coalesce(f.label, f.query)) LIKE '%%' || lower(%s) || '%%')
         ORDER BY f.last_used_at DESC NULLS LAST, f.created_at DESC
         LIMIT %s
        """,
        (principal_id, typed, typed, limit),
    )


def count_for(principal_id: str) -> int:
    """How many favorites the user has (for the modal hint)."""
    row = db.fetch_one(
        "SELECT count(*) AS n FROM query_favorites WHERE slack_user_id = %s",
        (principal_id,),
    )
    return int(row["n"]) if row else 0
