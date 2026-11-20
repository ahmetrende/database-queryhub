"""Two rules a super admin ran into on the same query.

1. A procedural block (DO / CALL) is a CAPABILITY refusal, not a structural
   one, so it lifts for a super admin — and lands at the DDL tier, because
   nothing in this codebase can see inside the block.
2. The engine's own catalog is metadata, not people, so PII masking does not
   run over it — for any engine.
"""
import pytest

from queryhub import engines, pii, query_safety as qs


# ---- 1. procedural blocks ---------------------------------------------------

DO_BLOCK = "DO $$ BEGIN CREATE ROLE dms_user WITH LOGIN; END $$;"


def test_a_procedural_block_is_refused_for_an_ordinary_user():
    r = qs.analyze(DO_BLOCK, engine="postgres")
    assert r.blocked
    assert any("DO" in b for b in r.blockers)


def test_a_super_admin_may_run_a_procedural_block():
    r = qs.analyze(DO_BLOCK, engine="postgres", unrestricted=True)
    assert not r.blocked, r.blockers


def test_a_procedural_block_is_classified_at_the_top_tier():
    """The body is opaque to every guard here — no keyword scan, no AST pass,
    no WHERE check reaches inside it. Classifying by what it looks like is how
    a block that creates a LOGIN role gets handed the read-only credential."""
    r = qs.analyze(DO_BLOCK, engine="postgres", unrestricted=True)
    assert r.main_tier == "ddl"


def test_call_gets_the_same_treatment():
    assert qs.analyze("CALL p(1);", engine="postgres").blocked
    r = qs.analyze("CALL p(1);", engine="postgres", unrestricted=True)
    assert not r.blocked and r.main_tier == "ddl"


@pytest.mark.parametrize("sql", [
    "COMMIT;", "ROLLBACK;", "BEGIN;", "SAVEPOINT s;", "RELEASE s;",
    "DISCARD ALL;", "RESET ALL;", "PREPARE p AS SELECT 1;", "DECLARE c CURSOR FOR SELECT 1;",
])
def test_structural_refusals_hold_even_for_a_super_admin(sql):
    """These are not permission questions. Transaction control would end the
    wrapper the executor opened (taking rollback-on-error and the audited
    statement boundary with it); RESET/DISCARD would throw away the SET LOCAL
    hardening — pinned search_path, statement timeout, role elevation; the
    prepared/cursor family would stop the reviewed text being the run text."""
    assert qs.analyze(sql, engine="postgres", unrestricted=True).blocked


def test_copy_stays_refused_for_a_super_admin():
    """Reaching the database server's filesystem is outside SQL, and stays a
    deliberate out-of-band act rather than something this gateway grants."""
    assert qs.analyze("COPY t TO '/tmp/x';", engine="postgres",
                      unrestricted=True).blocked


def test_the_super_admin_lists_are_exactly_these():
    """A guard on the per-engine lists: everything in them bypasses a refusal,
    so growing one is a security decision and should not happen by accident."""
    expected = {"postgres": frozenset({"DO", "CALL"}),
                "mssql": frozenset({"EXEC", "EXECUTE"})}
    for name, spec in engines._ENGINES.items():
        assert spec.super_admin_leading == expected.get(name, frozenset()), name
    assert qs.SUPER_ADMIN_LEADING == frozenset({"DO", "CALL"})


# ---- 1b. SQL Server EXEC ----------------------------------------------------

LOAD_DAY = "EXEC dbo.load_day @d = '2026-01-01'"


@pytest.mark.parametrize("sql,word", [
    (LOAD_DAY, "EXEC"),
    ("EXECUTE sp_help 'dbo.t'", "EXECUTE"),
])
def test_exec_is_refused_for_an_ordinary_user(sql, word):
    r = qs.analyze(sql, engine="mssql")
    assert r.blocked
    assert engines.MSSQL.banned_leading[word] in r.blockers


def test_a_super_admin_may_exec_a_procedure_at_the_top_tier():
    r = qs.analyze(LOAD_DAY, engine="mssql", unrestricted=True)
    assert not r.blocked, r.blockers
    assert r.main_tier == "ddl"


@pytest.mark.parametrize("sql", [
    "EXEC @rc = dbo.load_day 1",
    "EXEC dbo.load_day @at = 1",
    "EXEC dbo.sp_help 'dbo.t'",
])
def test_other_procedure_calls_run_at_the_top_tier(sql):
    """`dbo.sp_help` is a user procedure that borrows a system name, so only
    a bare or `sys.` name reads as the catalog procedure."""
    r = qs.analyze(sql, engine="mssql", unrestricted=True)
    assert not r.blocked, r.blockers
    assert r.main_tier == "ddl"


@pytest.mark.parametrize("sql", [
    "EXEC sp_help 'dbo.t'",
    "EXECUTE sys.sp_columns 't'",
    "EXEC SP_HELPINDEX 'dbo.t'",
])
def test_catalog_procedures_run_at_the_read_tier(sql):
    r = qs.analyze(sql, engine="mssql", unrestricted=True)
    assert not r.blocked, r.blockers
    assert r.main_tier == "ro"


@pytest.mark.parametrize("sql,reason", [
    ("EXECUTE AS USER = 'x'", "EXECUTE AS is not allowed"),
    ("EXEC AS LOGIN = 'sa'", "EXECUTE AS is not allowed"),
    ("EXEC ('select 1')", "Dynamic SQL"),
    ("EXEC (@sql)", "Dynamic SQL"),
    ("EXEC sp_executesql N'select 1'", "Dynamic SQL"),
    ("EXEC otherdb.dbo.p", "another database"),
    ("EXEC xp_cmdshell 'dir'", "`xp_cmdshell` is not allowed"),
    ("EXEC dbo.p AT srv", "linked server"),
    ("EXEC dbo.p WITH RESULT SETS NONE", "cannot read this EXEC"),
    # Found in review (measured on sqlglot 30.14): each of these passed once.
    ("EXEC sp_MSforeachtable 'DELETE FROM ?'", "read-only system procedures"),
    ("EXEC sp_MSforeachdb 'SELECT 1'", "read-only system procedures"),
    ("EXEC sys.sp_rename 'a', 'b'", "read-only system procedures"),
    ('EXEC dbo.p AT"lnk"', "linked server"),
    ("EXEC dbo.p AT [lnk]", "linked server"),
    ("EXEC otherdb..someproc", "another database"),
    ("EXEC dbo.p @a=1 EXEC xp_cmdshell 'dir'", "cannot read this EXEC"),
])
def test_exec_refusals_hold_for_a_super_admin(sql, reason):
    r = qs.analyze(sql, engine="mssql", unrestricted=True)
    assert r.blocked
    assert any(reason in b for b in r.blockers), r.blockers


def test_call_stays_refused_on_sql_server_for_a_super_admin():
    assert qs.analyze("CALL p(1)", engine="mssql", unrestricted=True).blocked


def test_a_prepared_execute_stays_refused_on_postgres_for_a_super_admin():
    r = qs.analyze("EXECUTE stmt;", engine="postgres", unrestricted=True)
    assert r.blocked
    assert qs.BANNED_LEADING_REASONS["EXECUTE"] in r.blockers


# ---- 2. system catalog is not PII -------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT rolname FROM pg_roles",
    "SELECT rolname FROM pg_catalog.pg_roles",
    "SELECT usename, query FROM pg_stat_activity",
    "SELECT column_name FROM information_schema.columns",
])
def test_catalog_only_queries_are_not_masked(sql):
    assert pii.system_catalog_only(sql, engine="postgres")


def test_a_join_onto_user_data_keeps_masking():
    """ALL, not ANY — a query that consults the catalog on its way to user
    rows is a query about user rows."""
    assert not pii.system_catalog_only(
        "SELECT c.relname, u.email FROM pg_class c JOIN users u ON true",
        engine="postgres")


def test_an_unqualified_user_table_named_like_a_catalog_view_is_not_exempt():
    """`information_schema.tables` is not on any default search path, so a bare
    `tables` is somebody's own table. Prefix-matching it would unmask user
    data — which is why the prefix set is per engine and holds only `pg_`."""
    assert not pii.system_catalog_only("SELECT * FROM tables", engine="postgres")


def test_unparseable_sql_keeps_masking_on():
    assert not pii.system_catalog_only("SELECT * FROM pg_roles WHERE",
                                       engine="postgres")


def test_it_works_for_another_engine_without_a_code_change():
    """The rule reads the engine spec, so T-SQL gets it from its own
    system_schemas — and its empty prefix set means no bare name is exempt."""
    assert pii.system_catalog_only("SELECT name FROM sys.objects", engine="mssql")
    assert not pii.system_catalog_only("SELECT name FROM dbo.customers",
                                       engine="mssql")


@pytest.mark.parametrize("sql", [
    "EXEC dbo.p @at = 1",          # a parameter named at
    "EXEC dbo.[at]",               # a procedure named at
    "EXEC dbo.p N'at'",            # the word in a string
    "EXEC dbo.p /* AT srv */ 1",   # the word in a comment
])
def test_the_word_at_elsewhere_is_not_a_linked_server(sql):
    r = qs.analyze(sql, engine="mssql", unrestricted=True)
    assert not any("linked server" in b for b in r.blockers), r.blockers


def test_insert_exec_stays_refused_for_everyone():
    """INSERT ... EXEC does not parse, so the AST pass refuses it. The
    stmt_guard change that skips a called procedure's body must not open it."""
    assert qs.analyze("INSERT INTO t EXEC dbo.p", engine="mssql").blocked
    assert qs.analyze("INSERT INTO t EXEC dbo.p", engine="mssql",
                      unrestricted=True).blocked
