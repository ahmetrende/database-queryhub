"""Live tests for the SQL Server path, against a real SQL Server.

The rest of the suite reaches the SQL Server code through mocks only. These
tests run the product's own code against a real server: `mssql_exec.connect`
and its TLS settings, the tier logins that `deploy/grant_mssql.sql` creates,
`executor._run_mssql` and its single transaction, the statement timeout, what
Stop does, the server's own statement count, the result file, masking and
`errors.scrub`.

They are opt-in, like tests/test_integration_db.py. Set QH_RUN_MSSQL=1 and
QH_MSSQL_SA_PASSWORD. QH_MSSQL_HOST and QH_MSSQL_PORT default to 127.0.0.1 and
1433. The module creates its own database and its own logins, and drops them
at the end. Use a server that exists only for tests. CI runs this module in
the `mssql` job, against a SQL Server 2022 service container.

No metadata database takes part. The code under test reads three things from
it, and each gets a fixed answer here: no availability-group host map, no
masking exemptions, and two rows of the column-name catalog that migration 088
seeds. Any other read fails the test at once (`_no_metadata_db`).
"""
import csv
import itertools
import os
import secrets
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from queryhub import (cancellation, db, errors, executor, mssql_exec, pii,
                           query_safety, stmt_guard, targets)
from queryhub import config as cfg

pytestmark = [
    pytest.mark.integration,
    pytest.mark.mssql,
    pytest.mark.skipif(
        not os.environ.get("QH_RUN_MSSQL"),
        reason="set QH_RUN_MSSQL=1 (with a test-only SQL Server) to run"),
]

HOST = os.environ.get("QH_MSSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("QH_MSSQL_PORT", "1433"))
DATABASE = "qh_live_test"
TIERS = ("ro", "rw", "ddl")
TARGET_ID = 900001
REQUESTER = "U0AB12CD34"

# The grant script names its logins after the product. The tests give them
# their own prefix, so they cannot touch real QueryHub logins on any server.
GRANT_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "grant_mssql.sql"
SCRIPT_LOGIN_PREFIX = "queryhub_"
LOGIN_PREFIX = "qh_live_"
DB_SCOPE_MARKER = "DATABASE SCOPE"

# Two rows of the column-name catalog, as migration 088 seeds them:
# (pattern, pii_type, match_type, exclude_tokens).
CATALOG = [("full_name", "name", "substring", ()),
           ("email_address", "email", "substring", ())]

# A read that runs for hours. The XOR stops the optimizer from computing the
# count without a scan, so only the statement timeout ends it.
SLOW_READ = ("SELECT COUNT_BIG(*) AS n FROM sys.all_objects a "
             "CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c "
             "CROSS JOIN sys.all_objects d "
             "WHERE (a.object_id ^ b.object_id ^ c.object_id ^ d.object_id) = 7")

_request_ids = itertools.count(900001)


class _MetadataDbForbidden(BaseException):
    """A SQL Server live test opened the metadata database."""


@pytest.fixture(scope="module", autouse=True)
def _no_metadata_db():
    """Fail at once when the code under test opens the metadata database.

    The integration marker turns off the conftest barrier, and the `mssql` job
    runs no metadata database. Without this fixture, a missed stub would wait
    for the pool timeout and then pass on a fallback. It raises BaseException,
    for the reason conftest gives: production code catches Exception around
    these reads.
    """
    def _refuse(*args, **kwargs):
        raise _MetadataDbForbidden(
            "a SQL Server live test opened the metadata database. Give the "
            "read a fixed answer in this module.")

    patch = pytest.MonkeyPatch()
    patch.setattr(db, "init_pool", _refuse)
    patch.setattr(db, "_pool", None, raising=False)
    yield
    patch.undo()


def _admin_connection(database="master"):
    """A sysadmin connection for the fixture work, in autocommit mode.

    It uses the product's connection string with every choice spelled out,
    because a module fixture runs before the conftest stub of bot_config.
    """
    import pyodbc
    return pyodbc.connect(
        mssql_exec.odbc_connection_string(
            HOST, PORT, database, "sa", os.environ["QH_MSSQL_SA_PASSWORD"],
            driver=mssql_exec._DEFAULT_DRIVER, trust_server_cert=True,
            multi_subnet_failover=False),
        autocommit=True, timeout=15)


def _grant_script(passwords):
    """deploy/grant_mssql.sql as two batches: server scope and database scope."""
    text = GRANT_SCRIPT.read_text().replace(SCRIPT_LOGIN_PREFIX, LOGIN_PREFIX)
    for tier in TIERS:
        text = text.replace(f"<STRONG_PASSWORD_{tier.upper()}>", passwords[tier])
    head, marker, tail = text.partition(DB_SCOPE_MARKER)
    assert marker, f"{GRANT_SCRIPT.name} has no {DB_SCOPE_MARKER} block"
    cut = head.rfind("\n") + 1          # the marker sits in a comment line
    return head[:cut], head[cut:] + marker + tail


def _drop_test_objects(cur):
    """Drop the test database and the test logins, if they exist."""
    import pyodbc
    cur.execute(
        f"IF DB_ID(N'{DATABASE}') IS NOT NULL BEGIN "
        f"ALTER DATABASE [{DATABASE}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
        f"DROP DATABASE [{DATABASE}]; END")
    # pyodbc pools connections, so a closed connection can keep its session.
    cur.execute("SELECT session_id FROM sys.dm_exec_sessions "
                "WHERE login_name LIKE ? ESCAPE '!'",
                LOGIN_PREFIX.replace("_", "!_") + "%")
    for (session_id,) in cur.fetchall():
        try:
            cur.execute(f"KILL {int(session_id)}")
        except pyodbc.Error:
            pass                         # the session ended on its own
    for tier in TIERS:
        login = LOGIN_PREFIX + tier
        cur.execute(f"IF SUSER_ID(N'{login}') IS NOT NULL DROP LOGIN [{login}]")


_FIXTURE_SQL = (
    "CREATE TABLE dbo.people (id int PRIMARY KEY, "
    "email_address nvarchar(200) NOT NULL UNIQUE, full_name nvarchar(100) NOT NULL)",
    "INSERT INTO dbo.people VALUES (1, N'jordan.ray@example.com', N'Jordan Ray')",
    "CREATE TABLE dbo.ledger (id int PRIMARY KEY, note nvarchar(100) NOT NULL)",
    "CREATE PROCEDURE dbo.touch_ledger AS "
    "INSERT INTO dbo.ledger (id, note) VALUES (0, N'written by a procedure')",
    "CREATE TABLE dbo.typed (id int PRIMARY KEY, at_7 datetime2(7), "
    "at_3 datetime2(3), legacy datetime, at_offset datetimeoffset(7), "
    "guid uniqueidentifier, amount decimal(18, 4), label nvarchar(60), "
    "contact nvarchar(200), blob varbinary(16), blob_text varbinary(64))",
)

# Latin-1, CJK and an emoji outside the Basic Multilingual Plane, which is a
# surrogate pair in the server's UTF-16.
LABEL = "Zoë Åsa 日本語 \U0001F600"


@pytest.fixture(scope="module")
def server():
    """A test database with the tier logins that the grant script creates."""
    if not os.environ.get("QH_MSSQL_SA_PASSWORD"):
        pytest.fail("set QH_MSSQL_SA_PASSWORD to the sa password of the test server")
    admin = _admin_connection()
    cur = admin.cursor()
    _drop_test_objects(cur)             # what an aborted run left behind
    cur.execute(f"CREATE DATABASE [{DATABASE}]")

    passwords = {t: f"Qh-{secrets.token_hex(12)}-Live1" for t in TIERS}
    server_scope, database_scope = _grant_script(passwords)
    cur.execute(server_scope)           # runs in master
    cur.execute(f"USE [{DATABASE}]")
    cur.execute(database_scope)
    for sql in _FIXTURE_SQL:
        cur.execute(sql)
    cur.execute(
        "INSERT INTO dbo.typed VALUES (1, '2026-07-30 15:29:39.3971278', "
        "'2026-07-30 15:29:39.397', '2026-07-30 14:54:36.520', "
        "'2026-07-30 15:29:39.3971278 +03:00', "
        "'6F9619FF-8B86-D011-B42D-00C04FC964FF', 12345.6700, ?, "
        "N'jordan.ray@example.com', 0x00FF10, "
        "CAST('jordan.ray@example.com' AS varbinary(64)))", LABEL)
    cur.execute("USE master")

    def execute(sql, *params):
        """Run `sql` as sysadmin in the test database. Returns its rows, if any."""
        cur.execute(f"USE [{DATABASE}]")
        try:
            cur.execute(sql, *params)
            return [tuple(r) for r in cur.fetchall()] if cur.description else None
        finally:
            cur.execute("USE master")

    def target(user):
        return targets.TargetServer(
            id=TARGET_ID, alias="prod-main", host=HOST, port=PORT,
            default_database=DATABASE, username=user, enabled=True, notes=None,
            engine="mssql")

    def request():
        return {"id": next(_request_ids), "database_name": DATABASE,
                "wants_result": True, "requester_slack_id": REQUESTER,
                "result_format": "csv"}

    def locks_held(tier):
        """Locks that the sessions of a tier login hold, other than the
        database lock that every session holds."""
        cur.execute("SELECT COUNT(*) FROM sys.dm_tran_locks l "
                    "JOIN sys.dm_exec_sessions s ON s.session_id = l.request_session_id "
                    "WHERE s.login_name = ? AND l.resource_type <> 'DATABASE'",
                    LOGIN_PREFIX + tier)
        return cur.fetchone()[0]

    yield SimpleNamespace(
        login=lambda tier: (LOGIN_PREFIX + tier, passwords[tier]),
        admin=_admin_connection, query=execute, execute=execute, target=target,
        request=request, locks_held=locks_held, cursor=cur)
    _drop_test_objects(cur)
    admin.close()


@pytest.fixture
def rw_showplan(server):
    """The RW login with the SHOWPLAN permission, for this test only.

    `stmt_guard` needs SHOWPLAN to count the statements of a batch. Without
    it, SQL Server refuses the count and rolls back the open transaction (see
    test_without_showplan_each_statement_drops_the_work_before_it).
    """
    login = LOGIN_PREFIX + "rw"
    server.execute(f"GRANT SHOWPLAN TO [{login}]")
    yield
    server.execute(f"REVOKE SHOWPLAN FROM [{login}]")


@pytest.fixture(autouse=True)
def bot_config(no_db_config, monkeypatch, tmp_path):
    """bot_config as the test server needs it, plus the fixed metadata answers.

    The test server has a self-signed certificate. The product connects to such
    a server when `mssql_trust_server_cert` is on, so that key is on here. A
    test can edit the returned dict, and every other key keeps its default.
    """
    defaults = cfg.get_setting          # the conftest stub
    overrides = {"mssql_trust_server_cert": "true"}

    def get_setting(key, default=None):
        return overrides[key] if key in overrides else defaults(key, default)

    monkeypatch.setattr(cfg, "get_setting", get_setting)
    monkeypatch.setattr(mssql_exec, "load_host_map", lambda: {})
    monkeypatch.setattr(pii, "_load_column_patterns", lambda: list(CATALOG))
    monkeypatch.setattr(pii, "_load_exemptions", lambda *args, **kwargs: [])
    monkeypatch.setattr(executor, "CSV_DIR", tmp_path)
    return overrides


def _execute(server, sql, tier, monkeypatch, *, timeout_sec=30, request=None):
    """Run `sql` the way the executor runs an approved SQL Server request.

    `_finalize` is the step after the transaction: it writes the metadata
    database and delivers the result. The stub keeps what it receives.
    """
    report = query_safety.analyze(sql, engine="mssql")
    assert not report.blocked, report.blockers
    assert report.main_tier == tier
    user, password = server.login(tier)
    finished = {}

    def _finalize(client, request, results, csv_paths, **kwargs):
        finished["results"] = results

    monkeypatch.setattr(executor, "_finalize", _finalize)
    executor._run_mssql(None, request or server.request(), server.target(user),
                        report.main_tier, report, user, password, timeout_sec,
                        1000, 10 * 1024 * 1024)
    return finished["results"]


def _csv_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def _shown(exc):
    """The error text the executor gives the requester for a pyodbc error."""
    return errors.scrub(f"{type(exc).__name__}: {exc}")


# --- the connection --------------------------------------------------------


@pytest.mark.parametrize("settings", [
    {},
    {"mssql_trust_server_cert": "true", "target_ssl_verify_hosts": "<host>"},
], ids=["shipped-default", "host-on-the-verify-list"])
def test_tls_checks_the_certificate_unless_bot_config_trusts_it(
        server, bot_config, settings):
    """The shipped default checks the server certificate. A host on
    `target_ssl_verify_hosts` gets the check even when the SQL Server default
    trusts certificates. The test server's certificate is self-signed, so
    both checks refuse it."""
    import pyodbc
    bot_config.clear()
    bot_config.update({key: HOST if value == "<host>" else value
                       for key, value in settings.items()})
    with pytest.raises(pyodbc.OperationalError) as caught:
        mssql_exec.connect(HOST, PORT, DATABASE, *server.login("ro"), timeout_sec=5)
    shown = _shown(caught.value)
    assert "certificate verify failed" in shown
    assert "[Microsoft]" not in shown and "ODBC Driver" not in shown


def test_a_trusted_certificate_still_gives_an_encrypted_connection(server):
    """Trust skips the certificate check only. The link stays encrypted, and
    the connection leaves the transaction to its caller."""
    conn = mssql_exec.connect(HOST, PORT, DATABASE, *server.login("ro"),
                              timeout_sec=7)
    try:
        assert conn.autocommit is False
        assert conn.timeout == 7
        cur = conn.cursor()
        cur.execute("SELECT @@SPID")
        session_id = cur.fetchone()[0]
    finally:
        conn.close()
    server.cursor.execute(
        "SELECT c.encrypt_option, s.program_name, s.login_name "
        "FROM sys.dm_exec_connections c "
        "JOIN sys.dm_exec_sessions s ON s.session_id = c.session_id "
        "WHERE c.session_id = ?", session_id)
    assert tuple(server.cursor.fetchone()) == ("TRUE", "QueryHub", LOGIN_PREFIX + "ro")


# --- reads, writes and the transaction -------------------------------------


def test_a_read_runs_on_the_ro_login_and_reaches_the_result_file(server, monkeypatch):
    (result,) = _execute(
        server, "SELECT SUSER_NAME() AS who, COUNT(*) AS n FROM dbo.people",
        "ro", monkeypatch)
    assert result.has_result_set and result.rowcount == 1
    assert _csv_rows(result.csv_path) == [["who", "n"], [LOGIN_PREFIX + "ro", "1"]]
    # The web grid reads these types. pyodbc gives Python types, not SQL names.
    assert result.col_types == {"who": "str(128)", "n": "int"}


@pytest.mark.parametrize("sql, tier, refusal", [
    ("INSERT INTO dbo.ledger (id, note) VALUES (11, N'tier probe')",
     "rw", "The INSERT permission was denied"),
    ("SELECT id, full_name INTO dbo.people_copy FROM dbo.people",
     "ddl", "CREATE TABLE permission denied"),
], ids=["insert", "select-into"])
def test_the_classified_tier_is_the_lowest_login_the_server_accepts(
        server, sql, tier, refusal):
    """Each login below the classified tier gets a refusal from the server,
    and the login of that tier runs the statement. SELECT INTO starts with
    SELECT but creates a table, so a read tier would get it wrong."""
    import pyodbc
    report = query_safety.analyze(sql, engine="mssql")
    assert not report.blocked and report.main_tier == tier
    for login_tier in TIERS:
        conn = mssql_exec.connect(HOST, PORT, DATABASE, *server.login(login_tier),
                                  timeout_sec=10)
        try:
            cur = conn.cursor()
            if TIERS.index(login_tier) < TIERS.index(tier):
                with pytest.raises(pyodbc.ProgrammingError, match=refusal):
                    cur.execute(sql)
            else:
                cur.execute(sql)
                assert cur.rowcount == 1
        finally:
            conn.rollback()
            conn.close()


def test_exec_is_refused_by_the_classifier_and_by_every_tier_login(server):
    import pyodbc
    report = query_safety.analyze("EXEC dbo.touch_ledger", engine="mssql")
    assert report.blocked
    assert report.blockers[0].startswith("EXEC / stored-procedure execution")
    for tier in TIERS:
        conn = mssql_exec.connect(HOST, PORT, DATABASE, *server.login(tier),
                                  timeout_sec=10)
        try:
            with pytest.raises(pyodbc.ProgrammingError,
                               match="The EXECUTE permission was denied"):
                conn.cursor().execute("EXEC dbo.touch_ledger")
        finally:
            conn.rollback()
            conn.close()
    assert server.query("SELECT COUNT(*) FROM dbo.ledger WHERE id = 0") == [(0,)]


def test_a_write_script_is_one_transaction(server, rw_showplan, monkeypatch):
    """The statements of a script commit together, and a statement that fails
    rolls back the statements before it."""
    import pyodbc
    results = _execute(
        server,
        "INSERT INTO dbo.ledger (id, note) VALUES (21, N'first'); "
        "UPDATE dbo.ledger SET note = N'second' WHERE id = 21",
        "rw", monkeypatch)
    assert [r.rowcount for r in results] == [1, 1]
    assert not any(r.has_result_set for r in results)
    assert server.query("SELECT note FROM dbo.ledger WHERE id = 21") == [("second",)]

    with pytest.raises(pyodbc.IntegrityError):
        _execute(server,
                 "INSERT INTO dbo.ledger (id, note) VALUES (31, N'first'); "
                 "INSERT INTO dbo.ledger (id, note) VALUES (31, N'same key')",
                 "rw", monkeypatch)
    # NOLOCK reads uncommitted rows, so an open transaction would show row 31.
    assert server.query(
        "SELECT COUNT(*) FROM dbo.ledger WITH (NOLOCK) WHERE id = 31") == [(0,)]
    assert server.locks_held("rw") == 0


def test_without_showplan_each_statement_drops_the_work_before_it(
        server, monkeypatch):
    """A defect, measured here so that a fix makes this test fail.

    The logins that deploy/grant_mssql.sql creates hold no SHOWPLAN
    permission. For each statement that reads a table, `stmt_guard` then gets
    "SHOWPLAN permission denied". SQL Server rolls back the open transaction
    on that error, and the guard lets the statement through. So the script
    commits only its last statement, and the result still shows a row count
    for each statement.
    """
    results = _execute(
        server,
        "INSERT INTO dbo.ledger (id, note) VALUES (61, N'first'); "
        "UPDATE dbo.ledger SET note = N'second' WHERE id = 61",
        "rw", monkeypatch)
    assert [r.rowcount for r in results] == [1, 0]
    assert server.query("SELECT COUNT(*) FROM dbo.ledger WHERE id = 61") == [(0,)]


# --- time limit and Stop ------------------------------------------------------


def test_the_statement_timeout_ends_a_long_write_and_rolls_it_back(
        server, rw_showplan, monkeypatch):
    """SQL Server keeps the transaction open after a client timeout. The
    executor closes the connection, and the close rolls the transaction back."""
    import pyodbc
    started = time.monotonic()
    with pytest.raises(pyodbc.OperationalError) as caught:
        _execute(server,
                 "INSERT INTO dbo.ledger (id, note) VALUES (41, N'before'); "
                 "INSERT INTO dbo.ledger (id, note) "
                 f"SELECT 42, CAST(n AS nvarchar(100)) FROM ({SLOW_READ}) AS slow",
                 "rw", monkeypatch, timeout_sec=3)
    took = time.monotonic() - started
    assert caught.value.args[0] == "HYT00"
    assert 2.5 <= took < 30, took
    assert "Query timeout expired" in _shown(caught.value)
    assert server.query("SELECT COUNT(*) FROM dbo.ledger WITH (NOLOCK) "
                        "WHERE id IN (41, 42)") == [(0,)]
    assert server.locks_held("rw") == 0


def test_stop_cannot_reach_a_running_query_and_the_timeout_ends_it(
        server, monkeypatch):
    """Stop on SQL Server: the executor records no backend for the run, so
    `stop_backend` has nothing to signal and reports FAILED. The query keeps
    running on the server, and only the statement timeout ends it."""
    import pyodbc
    report = query_safety.analyze(SLOW_READ, engine="mssql")
    assert not report.blocked and report.main_tier == "ro"
    user, password = server.login("ro")
    target, request = server.target(user), server.request()
    monkeypatch.setattr(executor, "_finalize", lambda *args, **kwargs: None)
    outcome = {}

    def run_request():
        started = time.monotonic()
        try:
            executor._run_mssql(None, request, target, "ro", report, user,
                                password, 6, 1000, 1024 * 1024)
        except Exception as exc:        # the timeout, which the test expects
            outcome["error"] = exc
        outcome["took"] = time.monotonic() - started

    worker = threading.Thread(target=run_request, daemon=True)
    worker.start()
    session_id = None
    for _ in range(50):
        found = server.query(
            "SELECT r.session_id FROM sys.dm_exec_requests r "
            "JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id "
            "CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t "
            "WHERE s.login_name = ? AND t.text LIKE '%CROSS JOIN sys.all_objects c%'",
            user)
        if found:
            session_id = found[0][0]
            break
        time.sleep(0.2)
    assert session_id, "the query did not start on the server"

    def requests_row(sql, params=None):
        # The requests row of a running SQL Server request: no backend pid.
        assert "SELECT backend_pid, engine_execution_id" in sql, sql
        return {"backend_pid": None, "engine_execution_id": None,
                "target_server_id": TARGET_ID, "executed_target_id": None,
                "database_name": DATABASE, "st": "executing"}

    monkeypatch.setattr(db, "fetch_one", requests_row)
    monkeypatch.setattr(targets, "get", lambda target_id: target)
    assert cancellation.stop_backend(request["id"]) == cancellation.CancelOutcome.FAILED
    assert server.query("SELECT COUNT(*) FROM sys.dm_exec_requests "
                        "WHERE session_id = ?", session_id) == [(1,)]

    worker.join(60)
    assert not worker.is_alive()
    assert isinstance(outcome.get("error"), pyodbc.OperationalError), outcome
    assert outcome["error"].args[0] == "HYT00"
    assert outcome["took"] >= 5.5, outcome


# --- the server's own statement count ---------------------------------------


def test_the_server_counts_statements_only_for_a_login_with_showplan(server):
    """`stmt_guard` compiles the batch with SHOWPLAN_XML and counts statements.
    T-SQL needs no semicolon between two statements, and the server counts both.
    The tier logins hold no SHOWPLAN permission, so for a statement that reads
    a table the server gives no count and the guard lets it through."""
    two_without_semicolon = "SELECT id FROM dbo.ledger DELETE FROM dbo.ledger WHERE id = 1"
    assert query_safety.analyze(two_without_semicolon, engine="mssql").blocked
    sysadmin = server.admin(DATABASE)
    try:
        cur = sysadmin.cursor()
        assert stmt_guard._mssql_statement_count(cur, "SELECT id FROM dbo.ledger") == 1
        assert stmt_guard._mssql_statement_count(cur, two_without_semicolon) == 2
        with pytest.raises(stmt_guard.TooManyStatements):
            stmt_guard.check(cur, two_without_semicolon, engine="mssql")
        cur.execute("SELECT 5 AS five")        # SHOWPLAN is off again
        assert [tuple(r) for r in cur.fetchall()] == [(5,)]
    finally:
        sysadmin.close()

    conn = mssql_exec.connect(HOST, PORT, DATABASE, *server.login("rw"), timeout_sec=10)
    try:
        cur = conn.cursor()
        assert stmt_guard._mssql_statement_count(cur, two_without_semicolon) is None
        stmt_guard.check(cur, two_without_semicolon, engine="mssql")
    finally:
        conn.rollback()
        conn.close()


# --- what reaches the requester -------------------------------------------------


def test_sql_server_types_reach_the_result_file_and_pii_is_masked(server, monkeypatch):
    (result,) = _execute(
        server,
        "SELECT id, at_7, at_3, legacy, at_offset, guid, amount, label, contact, "
        "blob, blob_text FROM dbo.typed", "ro", monkeypatch)
    header, row = _csv_rows(result.csv_path)
    assert header == ["id", "at_7", "at_3", "legacy", "at_offset", "guid",
                      "amount", "label", "contact", "blob", "blob_text"]
    assert row == [
        "1",
        "2026-07-30 15:29:39.397127",        # the 7th digit: past what Python holds
        "2026-07-30 15:29:39.397",           # trimmed to the declared scale
        "2026-07-30 14:54:36.520",
        "2026-07-30 15:29:39.397127+03:00",
        "6F9619FF-8B86-D011-B42D-00C04FC964FF",
        "12345.6700",
        LABEL,
        "j***@example.com",                  # the value scan
        "b'\\x00\\xff\\x10'",                # binary that is not text: Python's repr
        "j***@example.com",                  # the value scan reads UTF-8 in binary
    ]
    assert result.pii_masked == {"email"}

    # An alias does not hide a catalog column: the T-SQL parser finds its source.
    (aliased,) = _execute(server, "SELECT full_name AS shown FROM dbo.people",
                          "ro", monkeypatch)
    assert _csv_rows(aliased.csv_path) == [["shown"], ["J***** R**"]]
    assert aliased.pii_masked == {"name"}


def test_a_server_error_reaches_the_requester_scrubbed_and_masked(server, monkeypatch):
    """A unique-key error quotes the duplicate value. `errors.scrub` drops the
    driver prefix and masks the value, as it does for a result."""
    import pyodbc
    with pytest.raises(pyodbc.IntegrityError) as caught:
        _execute(server,
                 "INSERT INTO dbo.people (id, email_address, full_name) "
                 "VALUES (2, N'jordan.ray@example.com', N'Sam Doe')",
                 "rw", monkeypatch)
    raw = f"{type(caught.value).__name__}: {caught.value}"
    assert "(jordan.ray@example.com)" in raw
    shown = _shown(caught.value)
    assert "Cannot insert duplicate key in object 'dbo.people'" in shown
    assert "jordan.ray@example.com" not in shown
    assert "(j***@example.com)" in shown
    assert "[Microsoft]" not in shown and "[SQL Server]" not in shown
