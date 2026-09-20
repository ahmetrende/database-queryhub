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


def test_athena_spec():
    """Athena is the first engine with nothing to connect to: no host, no
    password, an API call against a workgroup. The spec carries only what the
    safety layer reads, and two of its fields are deliberate omissions."""
    a = engines.spec("athena")
    assert a.sqlglot_dialect == "athena"     # Trino family
    assert a.read_only is True
    assert a.driver == "athena"
    assert a.default_port == 443
    assert a.set_local_supported is False
    assert a.supports_explain is False       # Athena's EXPLAIN is not PG's
    assert a.routines_sql is None            # the catalog is Glue; no routines
    # A 3-part name in Athena names a CATALOG, which is how a federated
    # connector is reached. One approved target means one catalog.
    assert a.block_catalog_refs is True
    # Empty ON PURPOSE, not by omission: Athena engine v3 has no function that
    # reaches outside the catalog, and the one way to call a Lambda is a
    # statement prefix the read-only gate already refuses.
    assert a.blocked_functions == frozenset()
    # MUST stay the default. A spec is per engine; the Glue database is per
    # target, and a second archive arrives with its own.
    assert a.default_schema == "public"


def test_athena_is_wired_but_carries_no_credential():
    """Spec first, execution after — the order mssql went through, completed
    2026-09-20. What stays true either way is that this engine has nothing to
    connect with: the identity is an assumed role, so a caller must not read a
    missing password as "not provisioned yet"."""
    assert engines.is_executable("athena") is True
    assert engines.spec("athena").read_only is True
    assert engines.spec("athena").requires_credentials is False
    # The two engines that DO connect must keep saying so.
    assert engines.spec("postgres").requires_credentials is True
    assert engines.spec("mssql").requires_credentials is True
