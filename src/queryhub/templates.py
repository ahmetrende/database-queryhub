"""CRUD helpers for saved /sql query templates.

The schema lives in `migrations/038_query_templates.sql`. Templates
are personal by default; setting `is_shared = TRUE` promotes one to
workspace-wide visibility. Names are unique per owner (case-insensitive)
so the modal's "Save as template" field acts as save-or-overwrite.

The Slack-facing surface lives in `slack_app/subcommands.py`:
    /sql templates                 → list (owner's + shared)
    /sql template <name>           → resolve + open modal pre-filled
    /sql template delete <name>    → drop (owner only)
    /sql template share <name>     → toggle is_shared (owner only)
"""
from __future__ import annotations


from . import db


# Name validation pulled out so the modal-submit path and the
# slash-command path apply identical rules.
def name_error(name: str) -> str | None:
    """Return a human-facing complaint if `name` isn't a valid
    template name; None when it's fine. Mirrors the CHECK constraint
    on the column + a sane character set."""
    n = (name or "").strip()
    if not n:
        return "Template name cannot be empty."
    if len(n) > 64:
        return "Template name must be 64 characters or fewer."
    # Letters / digits / dash / underscore / dot / space — keep it
    # friendly to humans and copy-paste-safe.
    if any(c for c in n if not (c.isalnum() or c in "-_.: ")):
        return ("Template name may contain only letters, digits, "
                "spaces, dot, dash, colon, underscore.")
    return None


# ---------- writes ------------------------------------------------------


def save(*,
         owner_slack_id: str,
         name: str,
         query: str,
         target_server_id: int | None,
         database_name: str | None,
         description: str | None = None,
         is_shared: bool = False) -> dict:
    """Insert-or-overwrite the named template for one owner.
    ON CONFLICT (owner_slack_id, lower(name)) → UPDATE all editable
    columns + touch updated_at. Returns the resulting row."""
    return db.insert_returning(
        """
        INSERT INTO query_templates
            (name, description, query, target_server_id,
             database_name, owner_slack_id, is_shared)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (owner_slack_id, lower(name)) DO UPDATE
           SET query            = EXCLUDED.query,
               target_server_id = EXCLUDED.target_server_id,
               database_name    = EXCLUDED.database_name,
               description      = EXCLUDED.description,
               is_shared        = EXCLUDED.is_shared,
               updated_at       = NOW()
        RETURNING id, name, description, query, target_server_id,
                  database_name, owner_slack_id, is_shared,
                  created_at, updated_at, last_used_at, use_count
        """,
        (name.strip(), description, query, target_server_id,
         database_name, owner_slack_id, is_shared),
    )


def delete(owner_slack_id: str, name: str) -> bool:
    """Hard-delete the user's own template. Returns True if a row
    was removed (i.e. it existed and the caller was the owner)."""
    row = db.fetch_one(
        "DELETE FROM query_templates "
        " WHERE owner_slack_id = %s AND lower(name) = lower(%s) "
        "RETURNING id",
        (owner_slack_id, name.strip()),
    )
    return row is not None


def set_shared(owner_slack_id: str, name: str, is_shared: bool) -> bool:
    """Flip the share bit (owner only). Returns True on update."""
    row = db.fetch_one(
        "UPDATE query_templates "
        "   SET is_shared = %s, updated_at = NOW() "
        " WHERE owner_slack_id = %s AND lower(name) = lower(%s) "
        "RETURNING id",
        (is_shared, owner_slack_id, name.strip()),
    )
    return row is not None


def record_use(template_id: int) -> None:
    """Bump use_count + last_used_at. Called from the modal opener
    so we count "actually used" rather than "listed in /sql templates"."""
    db.execute(
        "UPDATE query_templates "
        "   SET use_count = use_count + 1, last_used_at = NOW() "
        " WHERE id = %s",
        (template_id,),
    )


# ---------- reads -------------------------------------------------------


def get_visible(*, owner_slack_id: str, name: str) -> dict | None:
    """Resolve `<name>` for a viewer: prefer the viewer's own template
    over a shared one with the same name. Returns None when no match."""
    return db.fetch_one(
        """
        SELECT id, name, description, query, target_server_id,
               database_name, owner_slack_id, is_shared,
               created_at, updated_at, last_used_at, use_count
          FROM query_templates
         WHERE lower(name) = lower(%s)
           AND (owner_slack_id = %s OR is_shared = TRUE)
         ORDER BY (owner_slack_id = %s) DESC
         LIMIT 1
        """,
        (name.strip(), owner_slack_id, owner_slack_id),
    )


def search_for_picker(owner_slack_id: str, typed: str = "",
                      limit: int = 100) -> list[dict]:
    """Flat list for the modal's external_select picker. Owner's own
    templates first, then shared ones (deduplicated). Filtered by
    case-insensitive substring match on `name` when typed is given.

    Each row: {id, name, owner_slack_id, target_alias, database_name,
               is_owned, is_shared}. The 'is_owned' bit lets the
    renderer show a small badge for shared/borrowed templates."""
    typed = (typed or "").strip()
    sql = """
        SELECT t.id,
               t.name,
               t.owner_slack_id,
               t.database_name,
               t.is_shared,
               (SELECT alias FROM target_servers
                 WHERE id = t.target_server_id)  AS target_alias,
               (t.owner_slack_id = %s)           AS is_owned
          FROM query_templates t
         WHERE (t.owner_slack_id = %s OR t.is_shared = TRUE)
           AND (%s = '' OR lower(t.name) LIKE '%%' || lower(%s) || '%%')
         ORDER BY (t.owner_slack_id = %s) DESC,
                  t.last_used_at DESC NULLS LAST,
                  lower(t.name)
         LIMIT %s
    """
    return db.fetch_all(sql, (owner_slack_id, owner_slack_id,
                              typed, typed, owner_slack_id, limit))


def get(template_id: int) -> dict | None:
    """Fetch by id. Used by the modal load-template action to resolve
    the picked option's value."""
    return db.fetch_one(
        """
        SELECT id, name, description, query, target_server_id,
               database_name, owner_slack_id, is_shared,
               created_at, updated_at, last_used_at, use_count
          FROM query_templates WHERE id = %s
        """,
        (template_id,),
    )


def list_for(owner_slack_id: str) -> dict[str, list[dict]]:
    """Two lists: the user's own templates and the shared ones
    (excluding their own, to avoid double-listing). Each list rendered
    by /sql templates."""
    own = db.fetch_all(
        """
        SELECT t.id, t.name, t.description,
               t.target_server_id, t.database_name, t.is_shared,
               t.use_count, t.last_used_at, t.updated_at,
               (SELECT alias FROM target_servers WHERE id = t.target_server_id) AS target_alias
          FROM query_templates t
         WHERE t.owner_slack_id = %s
         ORDER BY t.last_used_at DESC NULLS LAST, lower(t.name)
        """,
        (owner_slack_id,),
    )
    shared = db.fetch_all(
        """
        SELECT t.id, t.name, t.description,
               t.target_server_id, t.database_name, t.is_shared,
               t.owner_slack_id,
               t.use_count, t.last_used_at, t.updated_at,
               (SELECT alias FROM target_servers WHERE id = t.target_server_id) AS target_alias
          FROM query_templates t
         WHERE t.is_shared = TRUE
           AND t.owner_slack_id <> %s
         ORDER BY t.last_used_at DESC NULLS LAST, lower(t.name)
        """,
        (owner_slack_id,),
    )
    return {"own": own, "shared": shared}
