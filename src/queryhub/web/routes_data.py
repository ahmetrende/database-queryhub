"""Read endpoints: /connections, /saved, /history (API_CONTRACT.md).

Every route runs behind verify_session (deps.current_user) + the same
whitelist gate /sql applies. Grant filtering reuses
teams.effective_grant_for_user — the single authority the Slack bot
uses; nothing here re-implements access logic.
"""
from __future__ import annotations

import json
import logging

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import admins, auto_approve, db, errors, favorites, targets, teams
from .. import config as cfg
from . import deps, mapping

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(deps.block_pw_gate)])

# The control-plane DB is hidden from every listing, same as Slack.
_HIDDEN_DATABASES = {"postgres"}

# Ceiling on how many tables one database contributes to the /connections
# payload. 200 was far too low and failed silently: the biggest database in
# this fleet has ~2.7k relations, so ~2.5k of them were missing from the tree
# AND from autocomplete with nothing on screen to say so. Configurable, and
# the payload now reports when it truncated.
_DEFAULT_MAX_TABLES_PER_DB = 2000


def _max_tables_per_db() -> int:
    return max(50, cfg.get_int("web_max_tables_per_db",
                               _DEFAULT_MAX_TABLES_PER_DB))


def _alias_of(target_id: int | None) -> str | None:
    if target_id is None:
        return None
    t = targets.get(int(target_id))
    return t.alias if t else None


# ---- /connections -----------------------------------------------------------

def _catalog_databases(target_id: int) -> list[str]:
    rows = db.fetch_all(
        "SELECT DISTINCT database_name FROM schema_tables "
        "WHERE target_server_id = %s ORDER BY database_name",
        (target_id,),
    )
    return [r["database_name"] for r in rows
            if r["database_name"] not in _HIDDEN_DATABASES]


def _catalog_table_refs(target_id: int, database: str) -> list[dict]:
    """Tables as {s: schema, n: name}. The catalog has always recorded
    `schema_name` (schema_tables is UNIQUE on target+db+schema+table), but the
    API used to drop it, so the browser had to GUESS a schema — 'public' for
    Postgres, 'dbo' for SQL Server. That is wrong for most of this fleet: only
    a fifth of catalogued tables live in `public`, and two same-named tables in
    different schemas collapsed into one indistinguishable entry."""
    cap = _max_tables_per_db()
    rows = db.fetch_all(
        "SELECT schema_name, table_name FROM schema_tables "
        "WHERE target_server_id = %s AND database_name = %s "
        "  AND relkind IN ('table','partitioned','view','matview') "
        "ORDER BY schema_name, table_name LIMIT %s",
        (target_id, database, cap + 1),   # +1 so we can tell we hit the cap
    )
    if len(rows) > cap:
        log.info("connections: %s/%s has more than %d tables — list truncated",
                 target_id, database, cap)
        rows = rows[:cap]
    return [{"s": r["schema_name"], "n": r["table_name"]} for r in rows]


def _catalog_tables(target_id: int, database: str) -> list[str]:
    """Bare table names (autocomplete still matches on these)."""
    return [r["n"] for r in _catalog_table_refs(target_id, database)]


@router.get("/connections")
def connections(claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    uid = claims["sub"]
    is_admin = admins.is_admin(uid)
    # Admins can submit to DISABLED targets they hold a grant on (mirrors the
    # POST /queries guard), so their saved queries / history can reference a
    # disabled alias — and the UI then can't resolve the tab's connection and
    # shows "—". Surface JUST the disabled targets an admin actually uses
    # (saved + history), not every parked target in the fleet, so those tabs
    # resolve without flooding the picker. Non-admins see enabled targets only.
    used_disabled: set[int] = set()
    if is_admin:
        used_disabled = {
            r["tid"] for r in db.fetch_all(
                "SELECT DISTINCT target_server_id AS tid FROM query_favorites "
                "  WHERE slack_user_id = %s AND target_server_id IS NOT NULL "
                "UNION "
                "SELECT DISTINCT target_server_id AS tid FROM requests "
                "  WHERE requester_slack_id = %s AND target_server_id IS NOT NULL "
                "    AND status <> 'draft'",
                (uid, uid))
        }
    out = []
    for t in (targets.list_all() if is_admin else targets.list_enabled()):
        if not t.enabled and t.id not in used_disabled:
            continue
        grant = teams.effective_grant_for_user(uid, t.id)
        if grant is None:
            continue
        allowed = grant["allowed_databases"]  # None = all
        dbs = sorted(allowed) if allowed is not None else _catalog_databases(t.id)
        if not dbs:
            # Only fall back to the target's default DB when the grant is
            # UNRESTRICTED. A restricted grant that lists only hidden DBs
            # (e.g. {'postgres'}) must NOT expose the default_database the
            # user isn't actually granted — skip the target entirely.
            if allowed is None:
                dbs = [t.default_database]
            else:
                continue
        # Filter AFTER the fallback, never before. `default_database` is
        # `postgres` on most of this fleet, so a target whose catalog is empty
        # -- which is every target between being enabled and its first schema
        # snapshot -- used to fall back to exactly the database that is meant to
        # be invisible, and the earlier filter had already run. It reached a
        # user's sidebar that way. Filtering here covers both the catalog list
        # and the fallback with one pass.
        dbs = [d for d in dbs if d not in _HIDDEN_DATABASES]
        if not dbs:
            continue
        db_entries = []
        auto_ro_any = False
        for d in dbs:
            aa = auto_approve.effective_grant(uid, "ro",
                                              target_server_id=t.id,
                                              database_name=d) is not None
            auto_ro_any = auto_ro_any or aa
            refs = _catalog_table_refs(t.id, d)
            db_entries.append({
                "id": d,
                "name": d,
                "tier": grant["mode"].upper(),
                # `tables` stays a bare-name list (autocomplete matches on it);
                # `tableRefs` carries the real schema so the tree can group by
                # it and every generated reference is properly qualified
                # instead of guessing public/dbo.
                "tables": [r["n"] for r in refs],
                "tableRefs": refs,
                # Truthy when the list above is not the whole database, so the
                # UI can say so rather than imply the tree is complete.
                "tablesTruncated": len(refs) >= _max_tables_per_db(),
                "autoApproveRO": aa,
            })
        entry = {
            "id": t.alias,
            "name": t.alias,
            "engine": mapping.engine_label(getattr(t, "engine", None) or "postgres"),
            "env": mapping.env_of(t.alias),
            "autoApproveRO": auto_ro_any,
            "disabled": not t.enabled,
            "databases": db_entries,
        }
        # Endpoint, ADMINS ONLY. The database view shows it on hover so two
        # same-named databases can be told apart and the endpoint pasted into a
        # ticket. Never a credential and never a full connection string — but a
        # hostname is still infrastructure detail, and every requester does not
        # need the fleet's endpoints to write a query: the view already
        # distinguishes duplicates by SERVER NAME, and the design's own hover
        # falls back to it (`c.host || c.name`). So this is gated to the people
        # who administer the fleet rather than sent to everyone.
        if is_admin:
            entry["host"] = getattr(t, "host", None)
            entry["port"] = getattr(t, "port", None)
        out.append(entry)
    return {"connections": out}


# ---- /saved -----------------------------------------------------------------

class SavedIn(BaseModel):
    name: str | None = None
    connectionId: str | None = None
    databaseId: str | None = None
    sql: str = Field(min_length=1, max_length=100_000)


def _target_by_alias(alias: str | None) -> "targets.TargetServer | None":
    """Resolve an alias regardless of enabled state — callers decide
    whether a disabled target is acceptable (admins may use them, same
    as the Slack picker)."""
    if not alias:
        return None
    row = db.fetch_one("SELECT id FROM target_servers WHERE alias = %s",
                       (alias,))
    return targets.get(row["id"]) if row else None


@router.get("/saved")
def saved_list(claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    rows = db.fetch_all(
        "SELECT id, query, target_server_id, database_name, label "
        "FROM query_favorites WHERE slack_user_id = %s "
        "ORDER BY last_used_at DESC",
        (claims["sub"],),
    )
    return {"saved": [mapping.saved_entry(r, _alias_of) for r in rows]}


@router.post("/saved", status_code=201)
def saved_create(body: SavedIn, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    t = _target_by_alias(body.connectionId)
    row = favorites.add(
        principal_id=claims["sub"],
        query=body.sql.strip(),
        target_server_id=t.id if t else None,
        database_name=body.databaseId,
        label=(body.name or "").strip() or None,
    )
    return mapping.saved_entry(row, _alias_of)


@router.delete("/saved/{saved_id}", status_code=204)
def saved_delete(saved_id: int, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    if not favorites.delete(claims["sub"], saved_id):
        raise deps._error(404, "not_found", "No such saved query.")


# ---- /sessions (server-synced named workspaces) -----------------------------
#
# Only dest="server" sessions live server-side (web_saved_sessions, mig 064);
# dest="local" sessions stay in the browser and are never sent here. Upsert by
# (user, name). The 30-day retention job lives in cleanup_old_results.py.

class SessionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    tabs: list[dict] = []


@router.get("/sessions")
def sessions_list(claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    rows = db.fetch_all(
        "SELECT id, name, tabs, updated_at FROM web_saved_sessions "
        "WHERE slack_user_id = %s ORDER BY updated_at DESC",
        (claims["sub"],))
    return {"sessions": [mapping.session_entry(r) for r in rows]}


@router.put("/sessions")
def sessions_upsert(body: SessionIn, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    name = body.name.strip()
    if not name:
        raise deps._error(422, "validation", "Session name is required.")
    # Normalize each tab to the stored shape; accept either client (conn/db)
    # or contract (connectionId/databaseId) keys.
    tabs = [{
        "name": (t.get("name") or "Untitled query"),
        "sql": t.get("sql") or "",
        "connectionId": t.get("connectionId") or t.get("conn"),
        "databaseId": t.get("databaseId") or t.get("db"),
    } for t in (body.tabs or [])]
    row = db.fetch_one(
        "INSERT INTO web_saved_sessions (slack_user_id, name, tabs, updated_at) "
        "VALUES (%s, %s, %s::jsonb, NOW()) "
        "ON CONFLICT (slack_user_id, name) "
        "DO UPDATE SET tabs = EXCLUDED.tabs, updated_at = NOW() "
        "RETURNING id, name, tabs, updated_at",
        (claims["sub"], name, json.dumps(tabs)))
    return mapping.session_entry(row)


@router.delete("/sessions/{session_id}", status_code=204)
def sessions_delete(session_id: int, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    row = db.fetch_one(
        "DELETE FROM web_saved_sessions WHERE id = %s AND slack_user_id = %s "
        "RETURNING id", (session_id, claims["sub"]))
    if row is None:
        raise deps._error(404, "not_found", "No such session.")


# ---- /history ---------------------------------------------------------------

@router.get("/history")
def history(limit: int = 50, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    limit = max(1, min(int(limit), 200))
    rows = db.fetch_all(
        "SELECT id, query, target_server_id, database_name, status, "
        "       row_count, created_at, decided_by_slack_id, decided_by_name "
        # status <> 'draft': a reserved id from an open tab is not history.
        "FROM requests WHERE requester_slack_id = %s AND status <> 'draft' "
        "ORDER BY id DESC LIMIT %s",
        (claims["sub"], limit),
    )
    return {"history": [mapping.history_entry(r, _alias_of) for r in rows]}


# ---- schema tree + fleet search (v2 new features) ---------------------------

_SEARCH_MAX = 60


def _granted_db(uid: str, alias: str, dbname: str):
    """Resolve (target, grant) if the user may see `dbname` on `alias`, else
    (None, None). Same authority as /connections."""
    t = _target_by_alias(alias)
    if t is None or (not t.enabled and not _is_admin(uid)):
        return None, None
    grant = teams.effective_grant_for_user(uid, t.id)
    if grant is None:
        return None, None
    allowed = grant["allowed_databases"]
    if allowed is not None and dbname not in allowed:
        return None, None
    if dbname in _HIDDEN_DATABASES:
        return None, None
    return t, grant


def _is_admin(uid: str) -> bool:
    from .. import admins
    return admins.is_admin(uid)


@router.get("/connections/{conn}/databases/{dbname}/schema")
def db_schema(conn: str, dbname: str, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    from .. import pii
    t, _grant = _granted_db(claims["sub"], conn, dbname)
    if t is None:
        raise deps._error(404, "not_found", "Unknown or ungranted database.")

    trows = db.fetch_all(
        "SELECT id, schema_name, table_name, relkind, row_estimate, indexes, "
        "       foreign_keys "
        "FROM schema_tables WHERE target_server_id = %s AND database_name = %s "
        "ORDER BY schema_name, table_name", (t.id, dbname))
    crows = db.fetch_all(
        "SELECT c.table_id, c.column_name, c.data_type, c.not_null, c.is_pk "
        "FROM schema_columns c JOIN schema_tables st ON st.id = c.table_id "
        "WHERE st.target_server_id = %s AND st.database_name = %s "
        "ORDER BY c.table_id, c.ordinal", (t.id, dbname))
    cols_by_table: dict[int, list[dict]] = {}
    for c in crows:
        cols_by_table.setdefault(c["table_id"], []).append(c)

    tables, views = [], []
    for tr in trows:
        fks = mapping.fk_map(tr.get("foreign_keys"))
        columns = []
        for c in cols_by_table.get(tr["id"], []):
            name = c["column_name"]
            entry = {"name": name, "type": c["data_type"],
                     "nullable": not c["not_null"], "pk": bool(c["is_pk"]),
                     "pii": bool(pii.column_pii_map([name]))}
            if name in fks:
                entry["fk"] = fks[name]
            columns.append(entry)
        if tr["relkind"] in ("view", "matview"):
            # Views carry their columns too now: the editor built its column
            # list from `tables` only, so a view's columns never reached
            # autocomplete.
            views.append({"name": tr["table_name"], "schema": tr["schema_name"],
                          "columns": columns})
        else:
            tables.append({
                "name": tr["table_name"],
                "schema": tr["schema_name"],
                "approxRows": tr.get("row_estimate"),
                "columns": columns,
                "indexes": [mapping.parse_index(i) for i in (tr.get("indexes") or [])],
            })
    return {"tables": tables, "views": views}


# ---- server roles (SUPER-ONLY, live) ----------------------------------------
#
# Exposes a target's DB role names + attributes — sensitive, so it is
# super-admin only, ENFORCED here (the client's isSuper flag is a UI hint,
# never authority). Roles aren't in the schema-catalog snapshot, so we read
# pg_roles live over a short-lived RO, READ ONLY, TLS connection (passwords
# are never exposed — pg_roles redacts rolpassword). Lazy + client-cached,
# so it costs one tiny query only when a super opens the Roles branch.

@router.get("/connections/{conn}/roles")
def connection_roles(conn: str, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    if not admins.is_super_admin(claims["sub"]):
        raise deps._error(403, "forbidden", "Super-admin access required.")
    t = _target_by_alias(conn)
    if t is None:
        raise deps._error(404, "not_found", f"Unknown connection '{conn}'.")
    try:
        db_user, password = targets.get_credentials(t.id, "ro")
    except LookupError:
        raise deps._error(503, "server_error",
                          "No read credential configured for this connection.")
    try:
        with psycopg.connect(
            host=t.host, port=t.port, dbname=t.default_database,
            user=db_user, password=password, connect_timeout=3,
            sslmode="require", application_name="dba-slack-bot:web-roles",
            options="-c statement_timeout=3000",
        ) as cn, cn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                "SELECT rolname, rolsuper, rolcanlogin, rolreplication "
                "FROM pg_catalog.pg_roles ORDER BY rolname")
            rows = cur.fetchall()
    except psycopg.Error as e:
        raise deps._error(503, "server_error", errors.scrub(e))

    roles = []
    for rolname, sup, login, repl in rows:
        note = ("superuser" if sup else "replication" if repl
                else "login" if login else "group")
        roles.append({"name": rolname, "kind": "user" if login else "group",
                      "login": bool(login), "sup": bool(sup), "note": note})
    return {"roles": roles}


@router.get("/search")
def search(q: str = "", claims: dict = Depends(deps.current_user)):
    """Fleet-wide findcol: match db / table / column names across targets
    the caller is granted (schema-catalog backed)."""
    deps.require_whitelisted(claims)
    term = (q or "").strip()
    if len(term) < 2:
        return {"results": []}
    uid = claims["sub"]
    like = f"%{term}%"
    rows = db.fetch_all(
        "SELECT DISTINCT st.target_server_id, st.database_name, st.table_name, "
        "       c.column_name "
        "FROM schema_tables st "
        "LEFT JOIN schema_columns c ON c.table_id = st.id AND c.column_name ILIKE %s "
        "WHERE st.table_name ILIKE %s OR st.database_name ILIKE %s "
        "   OR c.column_name ILIKE %s "
        "ORDER BY st.database_name, st.table_name LIMIT 400",
        (like, like, like, like))
    # Scope to granted (target, db) and shape results; cap output.
    grant_cache: dict[tuple, object] = {}
    results, seen = [], set()
    for r in rows:
        t = targets.get(r["target_server_id"])
        if t is None:
            continue
        key = (t.id, r["database_name"])
        if key not in grant_cache:
            gt, _ = _granted_db(uid, t.alias, r["database_name"])
            grant_cache[key] = gt
        if grant_cache[key] is None:
            continue
        col = r.get("column_name")
        if col and term.lower() in col.lower():
            k = ("column", t.alias, r["database_name"], r["table_name"], col)
            kind, extra = "column", {"table": r["table_name"], "column": col}
        elif term.lower() in (r["table_name"] or "").lower():
            k = ("table", t.alias, r["database_name"], r["table_name"], None)
            kind, extra = "table", {"table": r["table_name"]}
        elif term.lower() in (r["database_name"] or "").lower():
            k = ("db", t.alias, r["database_name"], None, None)
            kind, extra = "db", {}
        else:
            continue
        if k in seen:
            continue
        seen.add(k)
        results.append({"kind": kind, "connectionId": t.alias,
                        "databaseId": r["database_name"], **extra})
        if len(results) >= _SEARCH_MAX:
            break
    return {"results": results}
