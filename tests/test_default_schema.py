"""A request's default schema (migration 216). PostgreSQL only.

It changes search_path and nothing else. These tests need no database."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from queryhub import core_submit as cs
from queryhub import executor, pre_flight, query_safety, targets
from queryhub.mcp_server import catalog
from queryhub.slack_app import notifications


@pytest.mark.parametrize("name", ["app", "_x", "App_1", "a$b", "a" * 63])
def test_the_name_rule_accepts_a_plain_identifier(name):
    assert query_safety.valid_default_schema(name)


@pytest.mark.parametrize("name", [
    "", None, "app;drop", "a b", "a" * 64, "1app", '"app"', "app\n", "app.x",
])
def test_the_name_rule_refuses_anything_else(name):
    assert not query_safety.valid_default_schema(name)


def test_an_empty_value_means_no_default_schema():
    assert cs.normalize_default_schema("") is None
    assert cs.normalize_default_schema("   ") is None
    assert cs.normalize_default_schema(None) is None
    assert cs.normalize_default_schema("  app ") == "app"


@pytest.mark.parametrize("engine", ["mssql", "athena", "clickhouse"])
def test_a_non_postgres_engine_refuses_a_default_schema(engine):
    out = cs.default_schema_error(SimpleNamespace(engine=engine), "app")
    assert isinstance(out, cs.Rejection) and out.field == "default_schema"
    assert out.message == ("A default schema works only on PostgreSQL. Remove it, "
                           "or write the schema in the statement.")


@pytest.mark.parametrize("engine", ["postgres", None])
def test_postgres_accepts_a_plain_name(engine):
    assert cs.default_schema_error(SimpleNamespace(engine=engine), "app") is None


def test_a_bad_name_is_refused_on_postgres():
    out = cs.default_schema_error(SimpleNamespace(engine="postgres"), "app;drop")
    assert isinstance(out, cs.Rejection)
    assert out.message == "The default schema must be one schema name, for example app."


def test_the_executor_path_is_unchanged_without_a_schema():
    assert executor.search_path_for("ro") == "pg_catalog, public"
    assert executor.search_path_for("rw", None) == "pg_catalog, public"
    assert executor.search_path_for("ddl", "") == "public, pg_catalog"


def test_the_executor_path_with_a_schema():
    assert executor.search_path_for("ro", "app") == 'pg_catalog, "app", public'
    assert executor.search_path_for("rw", "app") == 'pg_catalog, "app", public'
    assert executor.search_path_for("ddl", "app") == '"app", public, pg_catalog'


def test_the_executor_refuses_a_bad_schema():
    with pytest.raises(ValueError):
        executor.search_path_for("ro", 'app"; drop table x; --')


def test_the_pre_flight_path():
    assert (pre_flight._set_search_path_sql(None)
            == "SET LOCAL search_path = pg_catalog, public")
    assert (pre_flight._set_search_path_sql("app")
            == 'SET LOCAL search_path = pg_catalog, "app", public')
    with pytest.raises(ValueError):
        pre_flight._set_search_path_sql("a b")


def test_the_prepared_request_has_no_schema_by_default():
    assert "default_schema" in cs.REQUEST_RETURNING
    field = cs.Prepared.__dataclass_fields__["default_schema"]
    assert field.default is None


def test_the_approver_sees_the_schema(monkeypatch):
    target = targets.TargetServer(
        id=7, alias="prod-ledger", host="primary.example.test", port=5432,
        default_database="ledger", username="reader", enabled=True, notes=None,
        engine="postgres")
    monkeypatch.setattr(notifications.targets, "get", lambda tid: target)
    with_schema = notifications.request_context_md(
        {"target_server_id": 7, "database_name": "ledger", "default_schema": "app"})
    assert "*Schema:* `app`" in with_schema
    without = notifications.request_context_md(
        {"target_server_id": 7, "database_name": "ledger"})
    assert "Schema" not in without


def test_the_mcp_catalog_lists_schema():
    submit = next(t for t in catalog.listing() if t["name"] == "submit_query")
    prop = submit["inputSchema"]["properties"]["schema"]
    assert prop["type"] == "string"
    assert prop["description"].startswith("Optional. PostgreSQL only.")
    assert "schema" not in submit["inputSchema"].get("required", [])
