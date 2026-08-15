"""engines — per-engine spec resolution + fail-closed execution gating."""
from queryhub import engines


def test_unknown_and_null_fall_back_to_postgres():
    assert engines.spec("postgres").name == "postgres"
    assert engines.spec(None).name == "postgres"
    assert engines.spec("").name == "postgres"
    assert engines.spec("nonsense").name == "postgres"   # unknown → safe default


def test_postgres_spec_is_neutral():
    # Every classification field is None so query_safety uses its own
    # Postgres module constants — the postgres path is byte-identical.
    pg = engines.spec("postgres")
    assert pg.sqlglot_dialect == "postgres"
    assert pg.read_only is False
    assert pg.allowed_leading is None
    assert pg.banned_leading is None
    assert pg.rw_keywords is None
    assert pg.ddl_keywords is None
    assert pg.destructive_keywords is None
    assert pg.set_local_supported is True
    assert pg.supports_explain is True
    assert pg.blocked_functions == frozenset()
    assert pg.driver == "psycopg"


def test_mssql_spec():
    m = engines.spec("mssql")
    assert m.sqlglot_dialect == "tsql"
    assert m.read_only is False
    assert m.driver == "pyodbc"
    assert m.default_port == 1433
    assert m.set_local_supported is False
    assert m.supports_explain is False
    assert "SELECT" in m.allowed_leading and "DENY" in m.allowed_leading
    assert "EXEC" in m.banned_leading and "BULK" in m.banned_leading
    assert m.rw_keywords == frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})
    assert "DENY" in m.ddl_keywords and "CREATE" in m.ddl_keywords
    # rowset / cross-server / file functions that turn a SELECT into a
    # cross-server or file read must be blocked.
    for fn in ("openrowset", "openquery", "opendatasource", "xp_cmdshell"):
        assert fn in m.blocked_functions


def test_is_executable_fail_closed():
    assert engines.is_executable("postgres") is True
    assert engines.is_executable(None) is True    # legacy null → postgres
    assert engines.is_executable("") is True
    # mssql is now wired (pyodbc dispatch validated against a real AG).
    assert engines.is_executable("mssql") is True
    # clickhouse carries a safety spec but has NO wired execution path yet
    # → fail closed (never routed through the Postgres path).
    assert engines.is_executable("clickhouse") is False
    # spec still resolves for a not-yet-executable engine.
    assert engines.spec("clickhouse").read_only is True
