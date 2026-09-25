"""The split metadata roles, against a real database (SEC-ROLES).

These run in the second pass of CI's integration job, after
scripts/split_metadata_roles.py has moved every object to a NOLOGIN owner and
the rest of the integration suite has run as the DML-only runtime login. They
check what the split exists for, from the runtime's side, and that a table and
a view created later by the migrator get the same treatment.

Skipped unless BOT_DB_MIGRATOR_USER is set, which is the sign the roles are
split; a single-login install has nothing here to check.
"""
import os

import psycopg
import pytest

from queryhub import db, metadata_roles
from queryhub.config import ENV

pytestmark = [
    pytest.mark.integration,
    pytest.mark.split_roles,
    pytest.mark.skipif(not (os.environ.get("QH_RUN_INTEGRATION")
                            and os.environ.get("BOT_DB_MIGRATOR_USER")),
                       reason="needs QH_RUN_INTEGRATION=1 and split roles "
                              "(BOT_DB_MIGRATOR_USER)"),
]


def _refused(sql, params=None):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.transaction() as cur:
            cur.execute(sql, params)


def test_the_runtime_can_add_to_the_audit_log_and_read_it():
    with db.transaction() as cur:
        cur.execute("INSERT INTO audit_log (actor_slack_id, actor_name, action, details) "
                    "VALUES ('U0EXAMPLE001', 'split probe', 'split_probe', '{}') RETURNING id")
        assert cur.fetchone()["id"]


@pytest.mark.parametrize("sql", [
    "UPDATE audit_log SET action = 'tampered' WHERE action = 'split_probe'",
    "DELETE FROM audit_log WHERE action = 'split_probe'",
    "TRUNCATE audit_log",
    "UPDATE audit_log_reportable SET action = 'tampered'",
    "DELETE FROM audit_log_reportable",
])
def test_the_runtime_cannot_change_or_remove_audit_rows(sql):
    """Through the table or through a view over it."""
    _refused(sql)


@pytest.mark.parametrize("sql", [
    "CREATE TABLE public.split_probe_t (id int)",
    "SELECT * FROM schema_migrations",
    "ALTER TABLE requests ADD COLUMN split_probe int",
])
def test_the_runtime_runs_no_ddl_and_cannot_see_the_ledger(sql):
    _refused(sql)


def test_a_table_and_a_view_the_migrator_adds_later_get_the_same_treatment():
    owner = os.environ["BOT_DB_OWNER_ROLE"]
    with psycopg.connect(host=ENV.bot_db_host, port=ENV.bot_db_port,
                         dbname=ENV.bot_db_name, user=os.environ["BOT_DB_MIGRATOR_USER"],
                         password=os.environ.get("BOT_DB_MIGRATOR_PASSWORD", ""),
                         row_factory=psycopg.rows.dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('role', %s, false)", (owner,))
        cur.execute("CREATE TABLE split_probe_later (id bigserial PRIMARY KEY, v text)")
        cur.execute("CREATE VIEW split_probe_later_v AS SELECT id, v FROM split_probe_later")
        for stmt in metadata_roles.runtime_grants(cur, ENV.bot_db_user, owner):
            cur.execute(stmt)
        conn.commit()
        try:
            with db.transaction() as rcur:
                rcur.execute("INSERT INTO split_probe_later (v) VALUES ('x') RETURNING id")
                rcur.execute("UPDATE split_probe_later SET v = 'y'")
                rcur.execute("SELECT count(*) AS n FROM split_probe_later_v")
                assert rcur.fetchone()["n"] == 1
            _refused("UPDATE split_probe_later_v SET v = 'z'")
            assert metadata_roles.runtime_grants(cur, ENV.bot_db_user, owner) == []
        finally:
            cur.execute("DROP VIEW split_probe_later_v")
            cur.execute("DROP TABLE split_probe_later")
            conn.commit()
