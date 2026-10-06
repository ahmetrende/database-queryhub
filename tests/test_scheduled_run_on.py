"""GET /scheduled says where each run will go (design round 2026-10-06).

The Scheduled panel shows an "on primary" / "on replica" chip from `runOn` and
`replicaName`. Without the two fields it shows no chip, which is safe but hides
a choice the requester made.
"""
from queryhub.web import mapping

ALIASES = {7: "orders-db", 9: "orders-db-read-1"}


def _row(run_on):
    return {"id": 1, "query": "SELECT 1", "target_server_id": 7, "database_name": "orders",
            "scheduled_for": None, "created_at": None, "run_on": run_on}


def test_auto_is_the_default():
    got = mapping.scheduled_entry(_row(None), ALIASES.get)
    assert got["runOn"] == "auto" and got["replicaName"] is None


def test_the_primary_is_named_as_such():
    got = mapping.scheduled_entry(_row("primary"), ALIASES.get)
    assert got["runOn"] == "primary" and got["replicaName"] is None


def test_a_replica_carries_its_alias():
    got = mapping.scheduled_entry(_row("replica:9"), ALIASES.get)
    assert got["runOn"] == "replica" and got["replicaName"] == "orders-db-read-1"


def test_an_unreadable_replica_keeps_the_kind_and_drops_the_name():
    got = mapping.scheduled_entry(_row("replica:x"), ALIASES.get)
    assert got["runOn"] == "replica" and got["replicaName"] is None


def test_the_route_reads_the_column():
    import inspect
    from queryhub.web import routes_queries
    assert "run_on" in inspect.getsource(routes_queries.scheduled_list)
