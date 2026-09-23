"""ClickHouse: read-only safety, the native cursor, and the rules that keep a
sleeping service asleep.

The engine was a safety spec with no execution path. It executes now, over the
native protocol as a readonly=1 login that refuses every setting and has no
server-side limits, so three things carry the weight: the default-deny checks
at submit, a cursor that sends nothing a readonly login may not send and stops
the query itself, and a catalog that never wakes a service on a schedule.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from queryhub import ast_safety, cancellation, clickhouse_exec, engines
from queryhub import config as cfg
from queryhub import query_safety as qs


@pytest.fixture(autouse=True)
def _ast_on(monkeypatch):
    real = cfg.get_setting

    def fake(key, default=None):
        if key == "ast_safety_enabled":
            return "on"
        if key.endswith("_blocked_functions"):
            return default
        return real(key, default)
    monkeypatch.setattr(cfg, "get_setting", fake)


def blocked(sql):
    return ast_safety.check(sql, engine="clickhouse")


# --- submit-time safety -----------------------------------------------------------

def test_reads_pass_and_everything_else_is_refused():
    assert not qs.analyze("SELECT count() FROM ledger.trades", engine="clickhouse").blocked
    assert not qs.analyze("WITH x AS (SELECT 1) SELECT * FROM x", engine="clickhouse").blocked
    for w in ("INSERT INTO t VALUES (1)", "ALTER TABLE t DELETE WHERE 1",
              "ALTER TABLE t UPDATE x = 1 WHERE 1", "CREATE TABLE t (id UInt8) ENGINE = Memory",
              "DROP TABLE t", "TRUNCATE TABLE t", "OPTIMIZE TABLE t FINAL", "SET readonly = 0"):
        assert qs.analyze(w, engine="clickhouse").blocked, w


def test_everyday_clickhouse_sql_is_not_refused():
    for q in ("SELECT toStartOfHour(ts) AS h, count() FROM events GROUP BY h ORDER BY h",
              "SELECT uniqExact(user_id) FROM ledger.volumes WHERE day >= today() - 7",
              "SELECT * FROM ledger.candles FINAL WHERE symbol = 'BTC' LIMIT 10",
              "SELECT * FROM numbers(10)", "SELECT * FROM generate_series(1, 5)"):
        assert blocked(q) == [], q


def test_table_functions_are_default_deny():
    """Not a list of bad ones: a table function nobody has named is refused."""
    for q in ("SELECT * FROM merge('db', '^t')", "SELECT * FROM url('http://x', CSV)",
              "SELECT * FROM (SELECT * FROM remote('h', db.t)) z",
              "SELECT * FROM s3('http://x', 'CSV')", "SELECT * FROM file('/etc/passwd')"):
        assert blocked(q), q


def test_server_internals_are_refused():
    assert blocked("SELECT * FROM system.users")
    assert blocked("SELECT query FROM system.query_log")
    assert blocked("SELECT * FROM information_schema.tables")


def test_the_dictionary_family_is_refused_in_every_typed_form():
    for f in ("dictGet", "dictGetString", "dictGetUInt64", "dictGetOrDefault", "joinGet"):
        assert blocked(f"SELECT {f}('d', 'a', k) FROM e"), f


def test_an_inline_settings_clause_is_refused_but_the_word_in_a_literal_is_not():
    assert blocked("SELECT * FROM events SETTINGS max_threads = 1")
    assert blocked("SELECT * FROM events settings readonly=0")
    assert blocked("SELECT 'settings = x' AS note FROM events") == []
    assert blocked("SELECT 1 -- SETTINGS readonly = 0") == []


def test_an_operator_can_block_more_but_never_less(monkeypatch):
    q = "SELECT base64Encode(toString(email)) FROM users"
    assert blocked(q) == []
    real = cfg.get_setting
    monkeypatch.setattr(cfg, "get_setting", lambda k, d=None: (
        "base64encode" if k == "clickhouse_blocked_functions"
        else "on" if k == "ast_safety_enabled" else real(k, d)))
    assert blocked(q)


def test_postgres_is_untouched():
    assert ast_safety.check("SELECT * FROM numbers(10)", engine="postgres") == []
    assert not qs.analyze("UPDATE t SET x = 1 WHERE id = 2").blocked


def test_the_engine_executes_natively():
    spec = engines.spec("clickhouse")
    assert engines.is_executable("clickhouse")
    assert spec.read_only and spec.driver == "clickhouse" and spec.default_port == 9440
    assert not spec.set_local_supported


# --- the cursor -------------------------------------------------------------------

class FakeClient:
    """execute_iter yields the column list, then rows -- the driver's shape."""

    def __init__(self, columns, rows, block=None):
        self.columns, self.rows, self.block = columns, rows, block
        self.settings_sent = []
        self.disconnected = False
        self.connection = SimpleNamespace(socket=self)
        self._broken = threading.Event()

    def execute_iter(self, sql, params=None, with_column_types=False, query_id=None,
                     settings=None):
        self.settings_sent.append(settings)
        self.query_id = query_id

        def gen():
            yield self.columns
            for r in self.rows:
                yield r
            if self.block:
                if self._broken.wait(self.block):
                    raise EOFError("connection closed")
        return gen()

    def shutdown(self, how):
        self._broken.set()

    def disconnect(self):
        self.disconnected = True


@pytest.fixture
def fast_watch(monkeypatch):
    monkeypatch.setattr(clickhouse_exec, "_WATCH_TICK_SEC", 0.02)
    monkeypatch.setattr(clickhouse_exec, "_CANCEL_POLL_EVERY", 1)


def _cursor(monkeypatch, fake, **kw):
    monkeypatch.setattr(clickhouse_exec, "client", lambda *a, **k: fake)
    return clickhouse_exec.ClickHouseCursor("h", 9440, "ledger", "u", "p", **kw)


def test_the_cursor_describes_and_streams_and_sends_no_settings(monkeypatch):
    fake = FakeClient([("n", "UInt64"), ("t", "DateTime64(3)"), ("d", "Decimal(18, 2)")],
                      [(1, None, None), (2, None, None)])
    seen = []
    cur = _cursor(monkeypatch, fake, request_id=7, on_started=seen.append)
    cur.execute("SELECT n, t, d FROM x")
    assert [d[0] for d in cur.description] == ["n", "t", "d"]
    assert cur.description[0].type_display == "UInt64"
    # DateTime64(3) is written with three sub-second digits, not Python's six.
    assert cur.description[1][5] == 3 and cur.description[2][5] is None
    assert list(cur) == [(1, None, None), (2, None, None)]
    assert fake.settings_sent == [None]          # a readonly=1 login refuses any
    assert seen == [fake.query_id] and fake.query_id.startswith("queryhub-7-")
    cur.close()
    assert fake.disconnected


def test_the_deadline_stops_the_query_and_says_why(monkeypatch, fast_watch):
    fake = FakeClient([("n", "UInt64")], [(1,)], block=5)
    cur = _cursor(monkeypatch, fake, timeout_sec=0)
    cur.execute("SELECT n FROM x")
    t0 = time.monotonic()
    with pytest.raises(clickhouse_exec.ClickHouseStopped, match="ran past the 0s limit"):
        list(cur)
    assert time.monotonic() - t0 < 2
    cur.close()


def test_stop_reaches_the_query_through_the_watchdog(monkeypatch, fast_watch):
    fake = FakeClient([("n", "UInt64")], [], block=5)
    cur = _cursor(monkeypatch, fake, timeout_sec=60, is_cancelled=lambda: True)
    cur.execute("SELECT n FROM x")
    with pytest.raises(clickhouse_exec.ClickHouseStopped, match="Cancelled"):
        list(cur)
    cur.close()


def test_a_cancel_from_another_process_is_reported_as_cancelled(monkeypatch):
    """The KILL may land on another replica; the watchdog is what stops it."""
    monkeypatch.setattr(cancellation.targets, "get_credentials", lambda tid, mode: ("u", "p"))
    monkeypatch.setattr(clickhouse_exec, "kill_query",
                        lambda *a: (_ for _ in ()).throw(ConnectionError("other replica")))
    t = SimpleNamespace(id=1, host="h", port=9440, engine="clickhouse")
    assert cancellation._stop_clickhouse(5, t, "queryhub-5-abc") == cancellation.CancelOutcome.CANCELLED


def test_the_executor_routes_clickhouse_to_its_own_path():
    import inspect
    from queryhub import executor
    src = inspect.getsource(executor._run)
    assert 'target.engine == "clickhouse"' in src and "_run_clickhouse(" in src


# --- the catalog does not wake a sleeping service -------------------------------------

@pytest.fixture
def refresh(monkeypatch):
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "refresh_schema_catalog.py"
    spec = importlib.util.spec_from_file_location("refresh_schema_catalog", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fleet = [SimpleNamespace(id=1, alias="pg", engine="postgres", host="pg.example.internal"),
             SimpleNamespace(id=2, alias="ch-up", engine="clickhouse", host="up.example.cloud"),
             SimpleNamespace(id=3, alias="ch-idle", engine="clickhouse", host="idle.example.cloud"),
             SimpleNamespace(id=4, alias="ch-done", engine="clickhouse", host="done.example.cloud")]
    ran = []
    monkeypatch.setattr(mod.targets_mod, "list_enabled", lambda: fleet)
    monkeypatch.setattr(mod, "refresh_target", lambda t, only_database=None: ran.append(t.alias) or {})
    monkeypatch.setattr(mod.schema_catalog, "summary_is_ok", lambda s: (True, None))
    monkeypatch.setattr(mod.schema_catalog, "record_refresh",
                        lambda *a: {"alert": False, "recovered": False})
    monkeypatch.setattr(mod, "_announce", lambda n: None)
    monkeypatch.setattr(mod, "_attempted_within", lambda tid, hours: tid == 4)
    return mod, ran


def test_the_hourly_run_never_reads_clickhouse(refresh, monkeypatch):
    mod, ran = refresh
    monkeypatch.setattr("sys.argv", ["refresh"])
    mod.main()
    assert ran == ["pg"]


def test_the_fresh_run_reads_only_running_services_not_read_today(refresh, monkeypatch):
    mod, ran = refresh
    monkeypatch.setattr(mod, "_clickhouse_states", lambda: {
        "up.example.cloud": "running", "idle.example.cloud": "idle",
        "done.example.cloud": "running"})
    monkeypatch.setattr("sys.argv", ["refresh", "--clickhouse-when-fresh"])
    mod.main()
    assert ran == ["ch-up"]


def test_a_stale_snapshot_reads_nothing(refresh, monkeypatch):
    """A `running` an hour old may be asleep now; reading it would wake it."""
    mod, ran = refresh
    monkeypatch.setattr(mod, "_clickhouse_states", lambda: None)
    monkeypatch.setattr("sys.argv", ["refresh", "--clickhouse-when-fresh"])
    mod.main()
    assert ran == []


# --- import -----------------------------------------------------------------------

def _importer():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "import_targets_from_inventory.py"
    spec = importlib.util.spec_from_file_location("import_targets", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_clickhouse_services_are_planned_by_name_with_a_suffix_on_collision():
    mod = _importer()
    servers = [
        {"engine": "clickhouse", "db_instance_identifier": "prod-ledger",
         "endpoint": "a1.eu-central-1.aws.clickhouse.cloud", "is_deleted": False},
        {"engine": "clickhouse", "db_instance_identifier": "prod-orders",
         "endpoint": "b2.eu-central-1.aws.clickhouse.cloud", "is_deleted": False},
        {"engine": "clickhouse", "db_instance_identifier": "gone",
         "endpoint": "c3.eu-central-1.aws.clickhouse.cloud", "is_deleted": True},
        {"engine": "clickhouse", "db_instance_identifier": "here-already",
         "endpoint": "d4.eu-central-1.aws.clickhouse.cloud", "is_deleted": False},
        {"engine": "postgres", "db_instance_identifier": "pg-one",
         "endpoint": "pg-one.example.internal", "is_deleted": False},
    ]
    plan = mod.plan_clickhouse_imports(
        servers, existing_hosts={"d4.eu-central-1.aws.clickhouse.cloud"},
        existing_aliases={"prod-orders"})
    assert [(p["alias"], p["host"].split(".")[0]) for p in plan] == [
        ("prod-ledger", "a1"), ("prod-orders-ch", "b2")]


def test_the_importer_uses_the_native_port():
    assert _importer().CLICKHOUSE_PORT == 9440


# --- the connection test ------------------------------------------------------------

def test_the_connection_test_speaks_clickhouse(monkeypatch):
    from queryhub.web import routes_admin as ra
    monkeypatch.setattr(clickhouse_exec, "probe", lambda *a, **k: "26.4.1.2359")
    out = ra._probe("clickhouse", "h", 9440, "default", "u", "p")
    assert out["ok"] is True and out["serverVersion"] == "26.4.1.2359"


def test_an_aggregate_state_column_says_how_to_read_it(monkeypatch):
    """Measured on a live AggregatingMergeTree table: the driver has no reader
    for AggregateFunction(...) and raises mid-stream. The requester is told what
    to write instead of seeing the driver's error."""
    from clickhouse_driver import errors as ch_errors

    class Unreadable(FakeClient):
        def execute_iter(self, *a, **k):
            def gen():
                yield [("s", "AggregateFunction(argMax, Decimal(20, 8), Tuple(UInt64, UInt64))")]
                raise ch_errors.UnknownTypeError(
                    "Unknown type AggregateFunction(argMax, Decimal(20, 8), Tuple(UInt64, UInt64))")
            return gen()
    cur = _cursor(monkeypatch, Unreadable([], []))
    cur.execute("SELECT * FROM t")
    with pytest.raises(ValueError, match="argMaxMerge"):
        list(cur)
    cur.close()


def test_a_server_error_reaches_the_requester_without_its_stack_trace():
    """Measured: the driver's text is `Code: 160.`, the message, and twenty
    stack frames; the scrubber keeps the first line, which alone says nothing."""
    e = type("ServerException", (Exception,), {})(
        "Code: 160.\nDB::Exception: The maximum sleep time is 3000000 microseconds. "
        "Requested: 5000000 microseconds per block (of size 5). Stack trace:\n\n0. src/Common/Exception.cpp")
    e.code = 160
    e.message = str(e)
    assert clickhouse_exec.server_message(e) == (
        "Code 160: The maximum sleep time is 3000000 microseconds. "
        "Requested: 5000000 microseconds per block (of size 5).")


def test_the_freshness_check_reads_the_view_in_the_inventorys_own_time_zone(refresh):
    """Two traps, both found live before any target was enabled: the bot's login
    may read v_server but not the `servers` table under it, and `updated_at` is
    a naive Istanbul timestamp that reads three hours in the future against
    now() -- always "fresh", which is the answer that wakes services."""
    import inspect
    mod, _ran = refresh
    src = inspect.getsource(mod._clickhouse_states)
    assert "FROM v_server" in src and "NOT is_deleted" in src
    assert "AT TIME ZONE" in src and "BETWEEN" in src
    assert mod._INVENTORY_TZ == "Europe/Istanbul"


def test_catalog_flags_reach_the_bot_db_as_booleans(monkeypatch):
    """ClickHouse returns a comparison as UInt8; the bot DB's boolean[] refused
    the smallint[] a list of ints became, and the first live snapshot failed."""
    class C:
        def execute(self, sql, params=None, with_column_types=False):
            if "system.tables" in sql:
                return ([("db", "t", "MergeTree", 5, 100, "", "id")],
                        [("schema_name", "String"), ("table_name", "String"), ("engine", "String"),
                         ("total_rows", "UInt64"), ("total_bytes", "UInt64"),
                         ("partition_key", "String"), ("sorting_key", "String")])
            return ([("db", "t", 1, "id", "UInt64", 1, None, 1, 1)],
                    [("schema_name", ""), ("table_name", ""), ("ordinal", ""), ("column_name", ""),
                     ("data_type", ""), ("not_null", "UInt8"), ("default_expr", ""),
                     ("is_pk", "UInt8"), ("in_index", "UInt8")])

        def disconnect(self):
            pass
    monkeypatch.setattr(clickhouse_exec, "client", lambda *a, **k: C())
    tables, columns = clickhouse_exec.catalog_snapshot("h", 9440, "db", "u", "p")
    assert columns[0]["not_null"] is True and columns[0]["is_pk"] is True
    assert tables[0]["relkind"] == "r" and tables[0]["indexes"][0]["def"] == "ORDER BY (id)"
