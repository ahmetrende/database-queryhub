"""The replica clean-up on a real database: the rows go, the primary's stay.

The mocked tests in tests/test_catalog_replica_clear.py pin what the code does.
Only a database shows that the SQL does it. The two statements remove a read
replica's tables, its columns (through the cascade) and its functions. They
leave a target that people can pick alone, even when the code names it by
mistake. The audit row counts what went.

The fixture is a primary and its replica that no deployment has. Each test
makes them again and removes them at the end. The test that clears every
replica skips itself when another replica holds rows. So a stray run against a
live database never touches a real one.
"""
import os

import pytest

from queryhub import db, schema_catalog

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

PRIMARY = "replica-clear-fixture-primary"
REPLICA = "replica-clear-fixture-replica"


def _wipe() -> None:
    """Remove the fixture's targets and everything that points at them."""
    ids = [r["id"] for r in db.fetch_all(
        "SELECT id FROM target_servers WHERE alias IN (%s, %s)", (PRIMARY, REPLICA))]
    if not ids:
        return
    db.execute("DELETE FROM schema_functions WHERE target_server_id = ANY(%s)", (ids,))
    db.execute("DELETE FROM schema_tables WHERE target_server_id = ANY(%s)", (ids,))
    db.execute("DELETE FROM target_servers WHERE id = ANY(%s)", (ids,))


def _target(alias: str, replica_of: int | None = None) -> int:
    return db.insert_returning(
        "INSERT INTO target_servers (alias, host, default_database, username, "
        "  password_encrypted, enabled, replica_of) "
        "VALUES (%s, 'fixture.example.test', 'postgres', 'unused', 'unused', "
        "  FALSE, %s) RETURNING id",
        (alias, replica_of))["id"]


def _seed(target_id: int, databases: list[str]) -> None:
    """Two tables of two columns, and one function, in each database."""
    for database in databases:
        for name in ("orders", "payments"):
            table_id = db.insert_returning(
                "INSERT INTO schema_tables (target_server_id, database_name, "
                "  schema_name, table_name, relkind) "
                "VALUES (%s, %s, 'public', %s, 'table') RETURNING id",
                (target_id, database, name))["id"]
            for ordinal, column in enumerate(("id", "amount"), start=1):
                db.execute(
                    "INSERT INTO schema_columns (table_id, ordinal, column_name, data_type) "
                    "VALUES (%s, %s, %s, 'integer')", (table_id, ordinal, column))
        db.execute(
            "INSERT INTO schema_functions (target_server_id, database_name, "
            "  schema_name, routine_name, routine_kind) "
            "VALUES (%s, %s, 'public', 'total', 'function')", (target_id, database))


def _held(target_id: int) -> dict:
    return db.fetch_one(
        "SELECT (SELECT count(*) FROM schema_tables "
        "         WHERE target_server_id = %s) AS tables, "
        "       (SELECT count(*) FROM schema_columns c "
        "          JOIN schema_tables s ON s.id = c.table_id "
        "         WHERE s.target_server_id = %s) AS columns, "
        "       (SELECT count(*) FROM schema_functions "
        "         WHERE target_server_id = %s) AS functions",
        (target_id, target_id, target_id))


def _audit_mark() -> int:
    return db.fetch_one("SELECT coalesce(max(id), 0) AS m FROM audit_log")["m"]


def _audit_rows(target_id: int, after: int) -> list[dict]:
    return db.fetch_all(
        "SELECT details FROM audit_log WHERE action = 'schema_catalog_pruned' "
        "   AND (details->>'targetId')::int = %s AND id > %s ORDER BY id",
        (target_id, after))


@pytest.fixture
def fleet():
    _wipe()
    primary = _target(PRIMARY)
    replica = _target(REPLICA, replica_of=primary)
    _seed(primary, ["app"])
    _seed(replica, ["app", "ledger"])
    yield primary, replica
    _wipe()


def _named(target_id: int, alias: str, primary_alias: str | None) -> dict:
    return {"id": target_id, "alias": alias, "primary_alias": primary_alias}


def test_a_replica_loses_its_rows_and_the_primary_keeps_its_own(fleet):
    primary, replica = fleet
    assert _held(replica) == {"tables": 4, "columns": 8, "functions": 2}
    mark = _audit_mark()
    assert schema_catalog._clear_replica(_named(replica, REPLICA, PRIMARY)) == {
        "tables": 4, "columns": 8, "functions": 2}
    assert _held(replica) == {"tables": 0, "columns": 0, "functions": 0}
    assert _held(primary) == {"tables": 2, "columns": 4, "functions": 1}
    rows = _audit_rows(replica, mark)
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details.pop("why").startswith("read replica")
    assert details == {"target": REPLICA, "targetId": replica, "replicaOf": PRIMARY,
                       "databases": ["app", "ledger"], "tables": 4, "columns": 8,
                       "functions": 2}


def test_the_listing_names_the_replica_and_never_the_primary(fleet):
    primary, replica = fleet
    held = {r["id"]: r for r in schema_catalog.replicas_holding_catalog()}
    assert replica in held and primary not in held
    assert {k: held[replica][k] for k in ("alias", "primary_alias", "tables", "functions")} == {
        "alias": REPLICA, "primary_alias": PRIMARY, "tables": 4, "functions": 2}


def test_a_primary_named_by_mistake_loses_nothing(fleet):
    primary, _replica = fleet
    mark = _audit_mark()
    assert schema_catalog._clear_replica(_named(primary, PRIMARY, None)) is None
    assert _held(primary) == {"tables": 2, "columns": 4, "functions": 1}
    assert _audit_rows(primary, mark) == []


def test_clearing_every_replica_twice_removes_the_rows_once(fleet):
    primary, replica = fleet
    if [r for r in schema_catalog.replicas_holding_catalog() if r["id"] != replica]:
        pytest.skip("this database holds the rows of a real replica; "
                    "the clear-all path stays unrun here")
    mark = _audit_mark()
    assert [c["id"] for c in schema_catalog.clear_replica_catalog()] == [replica]
    assert schema_catalog.clear_replica_catalog() == []
    assert len(_audit_rows(replica, mark)) == 1
    assert _held(replica) == {"tables": 0, "columns": 0, "functions": 0}
    assert _held(primary) == {"tables": 2, "columns": 4, "functions": 1}
