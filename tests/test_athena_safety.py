"""Athena safety — what a read-only warehouse must refuse.

Athena is read-only in the sense that matters commercially (nobody writes to
the archive), but NOT in the sense the engine enforces: the same endpoint that
runs a SELECT will happily run `CREATE TABLE AS`, `INSERT INTO` or `UNLOAD`,
and each of those WRITES to S3 or to the Glue catalog. So "read-only" here is
a property of this gateway, not of the service, and these tests are what makes
it true.

Two cases are worth the measurement rather than the assumption (2026-09-19):

* `USING EXTERNAL FUNCTION … LAMBDA '…' SELECT f(x)` calls a Lambda, and the
  first instinct is that it hides inside a SELECT where a leading-word rule
  cannot see it. It does not: in Athena the clause is a statement PREFIX, so
  the leading word is `USING` and the ordinary gate refuses it. No new
  mechanism was needed, which is the reason this test exists — to keep that
  true if the parser's opinion ever changes.
* A federated connector is addressed by naming its catalog
  (`"lambda:fn".db.tbl`), which parses as an ordinary 3-part table name. The
  cross-database rule written for SQL Server refuses exactly that, so the
  engine spec opts into it instead of inventing a second rule.
"""
import pytest

from queryhub import ast_safety, query_safety


def _blockers(sql: str) -> list[str]:
    report = query_safety.analyze(sql, engine="athena")
    return list(report.blockers)


@pytest.mark.parametrize("sql", [
    "CREATE TABLE t AS SELECT 1",
    "INSERT INTO events SELECT * FROM events",
    "UNLOAD (SELECT 1) TO 's3://bucket/x/' WITH (format = 'PARQUET')",
    "MSCK REPAIR TABLE events",
    "CREATE EXTERNAL TABLE x (a int) LOCATION 's3://bucket/x/'",
    "DROP TABLE events",
    "ALTER TABLE events SET LOCATION 's3://elsewhere/'",
    "CREATE VIEW v AS SELECT 1",
])
def test_every_statement_that_writes_is_refused(sql):
    """Each of these writes to S3 or to the catalog. None of them is a read,
    and the archive is write-once by design."""
    assert _blockers(sql), f"expected a blocker for: {sql}"


def test_the_lambda_call_is_refused_by_its_leading_word():
    """The interesting one: it is a prefix, not a hidden call."""
    sql = ("USING EXTERNAL FUNCTION score(x INT) RETURNS INT "
           "LAMBDA 'my-fn' SELECT score(1)")
    blockers = _blockers(sql)
    assert blockers
    assert "read-only" in blockers[0].lower()


@pytest.mark.parametrize("sql", [
    "SELECT user_id, event_time FROM events WHERE month = '2026-05' LIMIT 10",
    "WITH recent AS (SELECT * FROM events WHERE month = '2026-05') "
    "SELECT count(*) FROM recent",
    'SELECT "available" FROM archive_db.events LIMIT 1',
])
def test_reads_are_accepted(sql):
    """Including the two-part `database.table` form, which stays inside the
    approved catalog."""
    assert _blockers(sql) == []


def test_a_catalog_reference_is_refused():
    """Three parts name a catalog, and a catalog is how a federated connector
    (a Lambda reading some other system) is reached."""
    out = ast_safety.check('SELECT * FROM "lambda:myfn".db.tbl', engine="athena")
    assert out and "blocked" in out[0].lower()


def test_the_refusal_names_a_catalog_and_not_a_server():
    """The rule is SQL Server's; the words must not be. Athena's console writes
    `awsdatacatalog.db.table` by default, so the most likely reader of this
    message is somebody who pasted a working query — telling them about
    another "database on the server" sends them looking for the wrong thing."""
    out = ast_safety.check("SELECT * FROM awsdatacatalog.archive_db.events",
                           engine="athena")
    assert out
    assert "catalog" in out[0].lower()
    assert "server" not in out[0].lower()
    # And the SQL Server wording is untouched for SQL Server.
    tsql = ast_safety.check("SELECT * FROM otherdb.dbo.t", engine="mssql")
    assert tsql and "another database on the server" in tsql[0]


def test_the_catalogs_own_metadata_is_off_limits():
    """information_schema is the engine's catalog, not the archive. The schema
    browser already shows what is there, from Glue."""
    assert _blockers("SELECT * FROM information_schema.columns")


def test_a_tagged_target_cannot_execute_yet():
    """Belt and braces: the safety profile is live, the execution path is not,
    and the second must not be inferred from the first."""
    from queryhub import engines
    assert engines.is_executable("athena") is False
