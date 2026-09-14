"""SQL Server (T-SQL) engine safety: tier classification + blocklist.

Pure static analysis (no DB) through the engine-aware query_safety /
ast_safety path with engine='mssql'. Tier assertions read `main_tier`
(set by the leading-word / classification layer, independent of the
optional AST pass); block assertions come from the leading-word banned
set or, for the rowset-function cases, the AST fn-check.
"""
import pytest

from dba_slack_bot import ast_safety
from dba_slack_bot import query_safety as qs


def _tier(sql):
    return qs.analyze(sql, engine="mssql").main_tier


def _blocked(sql):
    return qs.analyze(sql, engine="mssql").blocked


@pytest.fixture
def ast_on(monkeypatch):
    # Force the AST second-pass on regardless of bot_config so the
    # function-blocklist assertions are deterministic.
    monkeypatch.setattr(ast_safety, "is_enabled", lambda engine="postgres": True)
    yield


# --- tier classification ----------------------------------------------------

def test_select_is_ro():
    assert _tier("SELECT TOP 10 * FROM dbo.Users") == "ro"


def test_dml_is_rw():
    assert _tier("INSERT INTO dbo.t (a) VALUES (1)") == "rw"
    assert _tier("UPDATE dbo.t SET a = 1 WHERE id = 5") == "rw"
    assert _tier("DELETE FROM dbo.t WHERE id = 5") == "rw"


def test_ddl_is_ddl():
    assert _tier("CREATE TABLE dbo.t (id INT)") == "ddl"
    assert _tier("ALTER TABLE dbo.t ADD c INT") == "ddl"
    assert _tier("DROP TABLE dbo.t") == "ddl"
    assert _tier("TRUNCATE TABLE dbo.t") == "ddl"


def test_deny_is_ddl():
    # T-SQL DENY is a permission statement → DDL tier (main_tier is set by
    # the classifier before any AST pass runs).
    assert _tier("DENY SELECT ON dbo.t TO analyst") == "ddl"


# --- banned leading words (query_safety layer, AST-independent) --------------

def test_exec_is_banned():
    assert _blocked("EXEC sp_who")
    assert _blocked("EXECUTE dbo.SomeProc")
    assert _blocked("EXEC ('SELECT 1')")


def test_bulk_insert_banned():
    assert _blocked("BULK INSERT dbo.t FROM 'x.csv'")


def test_admin_verbs_banned():
    for sql in ("USE master", "BACKUP DATABASE d TO DISK = 'x'",
                "DBCC CHECKDB", "SHUTDOWN", "KILL 52", "RECONFIGURE"):
        assert _blocked(sql), sql


def test_update_without_where_blocked():
    assert _blocked("UPDATE dbo.t SET a = 1")


def test_plain_select_not_blocked(ast_on):
    assert not _blocked("SELECT id, name FROM dbo.Users WHERE id = 1")


# --- AST function blocklist (rowset / cross-server) --------------------------

def test_openrowset_family_blocked(ast_on):
    # A SELECT that reaches another server / a file via a rowset function
    # classifies as read but must be blocked by the engine fn-blocklist.
    assert _blocked("SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')")
    assert _blocked("SELECT * FROM OPENQUERY(linked, 'SELECT 1')")


# --- Cross-database / cross-server identifiers (SEC-XDB) ---------------------

def test_three_part_name_blocked(ast_on):
    # database.schema.object reaches another database on the same instance.
    assert _blocked("SELECT * FROM otherdb.dbo.Secrets")
    assert _blocked("UPDATE otherdb.dbo.accounts SET x = 1 WHERE id = 2")


def test_four_part_name_blocked(ast_on):
    # server.database.schema.object reaches a linked server.
    assert _blocked("SELECT * FROM srv.otherdb.dbo.t")
    assert _blocked("SELECT * FROM [srv].[db].[dbo].[t]")


def test_three_part_name_blocked_inside_subquery(ast_on):
    assert _blocked(
        "SELECT * FROM dbo.a WHERE id IN (SELECT id FROM otherdb.dbo.b)"
    )


def test_one_and_two_part_names_clean(ast_on):
    # object and schema.object stay in the connected database — allowed.
    assert not _blocked("SELECT * FROM Users WHERE id = 1")
    assert not _blocked("SELECT * FROM dbo.Users WHERE id = 1")


def test_crossdb_message_mentions_the_reference(ast_on):
    blockers = qs.analyze("SELECT * FROM otherdb.dbo.Secrets", engine="mssql").blockers
    assert any("otherdb" in b and "database" in b.lower() for b in blockers)


# --- Postgres stays byte-identical (default engine) --------------------------

def test_postgres_default_unaffected():
    # Same helpers, default engine → the Postgres path. DENY is NOT a PG
    # leading word, so it is rejected as unrecognized under postgres —
    # proving the mssql keyword sets did not leak into the default path.
    assert qs.required_mode("SELECT 1") == "ro"
    assert qs.analyze("DENY SELECT ON t TO u").blocked is True


def test_postgres_three_part_name_not_caught_by_xdb_rule(ast_on):
    # block_catalog_refs is False for Postgres (it can't address another
    # database in one connection), so the cross-DB rule must not fire.
    blockers = qs.analyze("SELECT * FROM mydb.public.t WHERE id = 1",
                          engine="postgres").blockers
    assert not any("cross-database" in b.lower() or "cross-server" in b.lower()
                   for b in blockers)
