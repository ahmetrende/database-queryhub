"""Read-only access to the existing DBA `inventory` database that lives on
the same RDS as the bot DB. Currently used by the modal's database
typeahead — we ask `v_all_databases` for the list of DBs on a given target
endpoint, instead of opening a connection to each target ourselves.

Connects with the bot's own credentials (queryhub user) but with
`dbname='inventory'`. Requires `GRANT SELECT ON v_all_databases TO queryhub;`
on the inventory DB (one-time DBA action).
"""
from __future__ import annotations

import logging

import psycopg

from .config import ENV

log = logging.getLogger(__name__)


def list_databases_for_endpoint(endpoint: str) -> list[str]:
    """Return database_name values from inventory.v_all_databases for the
    given target host. Excludes 'postgres'. Returns an empty list on any
    error (logged as warning) so the modal doesn't crash."""
    if not endpoint:
        return []
    try:
        with psycopg.connect(
            host=ENV.bot_db_host,
            port=ENV.bot_db_port,
            dbname="inventory",
            user=ENV.bot_db_user,
            password=ENV.bot_db_password,
            connect_timeout=5,
            application_name="dba-slack-bot:inventory-lookup",
        ) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT database_name FROM v_all_databases "
                "WHERE endpoint = %s AND database_name <> 'postgres' "
                "ORDER BY database_name",
                (endpoint,),
            )
            return [r[0] for r in cur.fetchall()]
    except psycopg.Error as e:
        log.warning(
            "inventory lookup failed for endpoint=%s: %s",
            endpoint, e,
        )
        return []
