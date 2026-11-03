"""Which databases a person may see on a server.

One rule for every door. The web UI, the MCP tools and the submit check all ask
this module, so a person sees the same list everywhere.

The rule for one server and one person:

1. A restricted grant lists its `allowed_databases`.
2. An unrestricted grant lists the catalogued databases of the server. When the
   catalog is empty, it lists the `default_database`.
3. `HIDDEN_DATABASES` leave both lists. The filter runs after the fallback.
   The default database is `postgres` on most servers, so a filter before the
   fallback shows exactly the database that must stay hidden.
4. A server with no database left does not show.

This module imports no Slack and no web code, so every door can use it.
"""
from __future__ import annotations

from . import db

# The control-plane database. It is hidden from every listing.
HIDDEN_DATABASES = frozenset({"postgres"})

# How many usable databases a refusal names before it says "+N more".
USABLE_SHOWN = 8


def no_access_message(alias: str, database: str,
                      usable: list[str] | None = None) -> str:
    """The one answer for a database the person may not use.

    It is the same for a database that does not exist and for one that exists
    but is outside the grant. A different answer would tell the person which
    database names exist on the server.

    `usable` is the person's own list (`usable_databases`). The answer names
    those databases, so a person who made a typo sees the correct name. The
    list is the same for every refused name, and it holds only databases that
    the person may use. So it tells nothing about the server.
    """
    msg = f"You have no access to database `{database}` on `{alias}`."
    if usable:
        shown = ", ".join(f"`{d}`" for d in usable[:USABLE_SHOWN])
        more = (f" (+{len(usable) - USABLE_SHOWN} more)"
                if len(usable) > USABLE_SHOWN else "")
        msg += f" You can use: {shown}{more}."
    return msg


def usable_databases(target, grant: dict) -> list[str]:
    """The databases that this grant lets the person use on this server.

    The same list as `visible_databases`. This function reads the catalog only
    for an unrestricted grant, because a restricted grant lists its own
    databases.
    """
    catalog = (catalog_databases(target.id)
               if grant["allowed_databases"] is None else [])
    return visible_databases(target, grant, catalog)


def catalog_databases(target_id: int) -> list[str]:
    """The catalogued databases of one server, without the hidden ones."""
    rows = db.fetch_all(
        "SELECT DISTINCT database_name FROM schema_tables "
        "WHERE target_server_id = %s ORDER BY database_name",
        (target_id,),
    )
    return [r["database_name"] for r in rows
            if r["database_name"] not in HIDDEN_DATABASES]


def catalog_databases_map(target_ids: list[int]) -> dict[int, list[str]]:
    """`catalog_databases` for many servers in one query.

    A server with no snapshot is absent from the map. The single read returns
    [] for it.
    """
    ids = list(dict.fromkeys(int(t) for t in target_ids))
    if not ids:
        return {}
    out: dict[int, list[str]] = {}
    for r in db.fetch_all(
            "SELECT DISTINCT target_server_id AS t, database_name AS d "
            "  FROM schema_tables WHERE target_server_id = ANY(%s) "
            " ORDER BY target_server_id, database_name", (ids,)):
        if r["d"] in HIDDEN_DATABASES:
            continue
        out.setdefault(r["t"], []).append(r["d"])
    return out


def visible_databases(target, grant: dict, catalog: list[str]) -> list[str]:
    """The databases this grant shows on this server. [] means hide the server.

    `catalog` is the server's catalogued databases (`catalog_databases_map`).
    """
    allowed = grant["allowed_databases"]  # None = all
    dbs = sorted(allowed) if allowed is not None else list(catalog)
    if not dbs:
        # Fall back to the default database only for an UNRESTRICTED grant. A
        # restricted grant that lists only hidden databases must not show the
        # default database. The person holds no grant on it.
        if allowed is not None:
            return []
        dbs = [target.default_database]
    return [d for d in dbs if d and d not in HIDDEN_DATABASES]


def grant_covers(grant: dict, database: str) -> bool:
    """Whether the grant names this database and the name is not hidden."""
    allowed = grant["allowed_databases"]
    if allowed is not None and database not in allowed:
        return False
    return database not in HIDDEN_DATABASES
