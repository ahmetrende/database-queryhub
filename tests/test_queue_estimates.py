"""Queue items carry the row estimate and the tables of the stored plan.

PLA-1592 U9. `estRows` was always null and `estTables` always [] on every queue
item. The plan that the submit path stores in `requests.explain_plan` already
holds both: the estimate of the top node, and a "Relation Name" on every node
that reads a table. `pre_flight.plan_estimate` reads them out. It is pure, so
these tests feed it real plans: tests/corpus/pg17_plans.json holds what
PostgreSQL 17 printed for each statement, with the EXPLAIN options of the
submit path.
"""
import json
from pathlib import Path

import pytest

from queryhub import pre_flight
from queryhub.web import mapping

CORPUS = json.loads((Path(__file__).parent / "corpus" / "pg17_plans.json")
                    .read_text(encoding="utf-8"))


def _plan(name):
    return CORPUS[name]["plan"]


@pytest.mark.parametrize("name,rows,tables", [
    # A join: the estimate of the join, and both tables in plan order.
    ("join", 20287, ["orders", "customers"]),
    ("point", 1, ["customers"]),
    # LIMIT caps the estimate. The scan under it estimates far more.
    ("limit", 50, ["orders"]),
    # An InitPlan and a correlated subquery: their tables count too, once each.
    ("subquery", 4997, ["payouts", "orders", "customers"]),
    # A CTE scan names no table. The CTE's own plan does.
    ("cte", 1, ["orders", "customers"]),
    # Each partition is its own relation in the plan.
    ("partitions", 7, ["events_2026_09", "events_2026_10"]),
    # A write: ModifyTable estimates 0 rows out, so the rows come from its
    # input, the same number as the "~N rows affected" hint.
    ("update", 6, ["payouts"]),
    ("delete", 6, ["orders"]),
    ("insert_values", 1, ["payouts"]),
    # No table at all.
    ("function_only", 3, []),
])
def test_a_real_plan_gives_its_rows_and_tables(name, rows, tables):
    assert pre_flight.plan_estimate(_plan(name)) == (rows, tables)


def test_the_corpus_is_what_the_submit_path_stores():
    """Guard the guard: a corpus of empty or foreign plans would make every
    case above prove nothing."""
    assert len([k for k in CORPUS if not k.startswith("_")]) == 10
    for name, case in CORPUS.items():
        if name.startswith("_"):
            continue
        assert isinstance(case["plan"], list) and "Plan" in case["plan"][0], name
    assert "pre_flight.explain" in CORPUS["_about"]


def test_the_write_estimate_matches_the_hint_the_approver_reads():
    hint = pre_flight._write_estimate_text("UPDATE", _plan("update"))
    rows, _tables = pre_flight.plan_estimate(_plan("update"))
    assert hint is not None and f"~{rows} rows affected" in hint


def test_a_plan_stored_as_json_text_reads_the_same():
    assert pre_flight.plan_estimate(json.dumps(_plan("join"))) == (20287, ["orders", "customers"])


@pytest.mark.parametrize("stored", [
    None,
    [],
    "",
    "not json",
    {"Plan": {"Plan Rows": 3}},            # a dict, not PostgreSQL's list
    [None],
    ["text"],
    [{}],
    # The marker the submit path stores instead of a plan over 64 KB.
    [{"_truncated": True, "_original_size_bytes": 70000,
      "_note": "EXPLAIN plan exceeded 64KB cap; not stored."}],
    # A node without an estimate is not a plan this reads.
    [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "orders"}}],
    # Junk inside an otherwise valid root.
    [{"Plan": {"Node Type": "ModifyTable", "Plan Rows": 0, "Plans": [7]}}],
    [{"Plan": {"Node Type": "Seq Scan", "Plan Rows": "many"}}],
])
def test_anything_that_is_not_a_plan_gives_no_estimate(stored):
    assert pre_flight.plan_estimate(stored) == (None, [])


def test_a_plan_is_not_changed_by_reading_it():
    plan = _plan("subquery")
    before = json.dumps(plan, sort_keys=True)
    pre_flight.plan_estimate(plan)
    assert json.dumps(plan, sort_keys=True) == before


# ---- the queue item ----------------------------------------------------------

def _row(**over):
    row = {"id": 2051, "requester_slack_id": "U0EXAMPLE001",
           "requester_name": "Jordan Ray", "target_server_id": 3,
           "database_name": "app", "query": "SELECT c.email FROM orders o "
           "JOIN customers c ON c.id = o.customer_id", "justification": "why",
           "risk_summary": None, "created_at": None, "engine": "postgres",
           "explain_plan": _plan("join")}
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _no_pii_catalog(monkeypatch):
    from queryhub import pii
    monkeypatch.setattr(pii, "column_pii_map", lambda cols: {})


def test_the_queue_item_carries_the_estimate_of_its_plan():
    item = mapping.queue_item(_row(), lambda tid: "prod-main")
    assert item["estRows"] == 20287
    assert item["estTables"] == ["orders", "customers"]


def test_a_queue_item_without_a_plan_keeps_null_and_empty():
    """The live state: `query_plan_logging` is off, so no row stores a plan."""
    item = mapping.queue_item(_row(explain_plan=None), lambda tid: "prod-main")
    assert item["estRows"] is None and item["estTables"] == []


@pytest.mark.parametrize("engine", ["mssql", "clickhouse", "athena"])
def test_other_engines_give_null_and_empty_even_with_a_plan(engine):
    """Only a PostgreSQL-style EXPLAIN gives this shape. Whatever another
    engine's row holds, the answer stays what it was."""
    item = mapping.queue_item(_row(engine=engine), lambda tid: "prod-main")
    assert item["estRows"] is None and item["estTables"] == []


def test_a_row_with_no_engine_is_read_as_postgres():
    """Rows from before the engine column hold NULL, and every one of them
    ran on PostgreSQL."""
    assert mapping.queue_estimate(_row(engine=None)) == (20287, ["orders", "customers"])


def test_the_queue_reads_the_plan_column():
    """The mapper can only use what the queue SELECT reads."""
    from queryhub.web import routes_admin
    assert "explain_plan" in routes_admin._QUEUE_COLS
