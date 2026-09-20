"""The Athena cursor adapter — what the executor is allowed to assume.

Athena is asynchronous and paged; the executor is written against a cursor.
Adapting one to the other is cheap, and every one of these tests exists because
getting it subtly wrong would be invisible in the result and expensive in the
audit trail.
"""
import pytest

from queryhub import athena_exec

_CFG = {"region": "eu-central-1", "workgroup": "wg", "catalog": "AwsDataCatalog",
        "database": "archive_db", "role_arn": "arn:aws:iam::111111111111:role/r"}

_META = [{"Name": "user_id", "Type": "varchar"},
         {"Name": "available", "Type": "decimal", "Precision": 38, "Scale": 18}]


def _rows(*values):
    return [{"Data": [{"VarCharValue": v} if v is not None else {} for v in row]}
            for row in values]


class _FakeAthena:
    """Enough of the Athena client to exercise the adapter, and a record of
    every call so the tests can assert on what was SENT, not only received."""

    def __init__(self, states=("SUCCEEDED",), pages=None, reason=None):
        self.states = list(states)
        self.pages = pages or []
        self.reason = reason
        self.started: list[dict] = []
        self.stopped: list[str] = []
        self._page = 0

    def start_query_execution(self, **kw):
        self.started.append(kw)
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, **_kw):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        status = {"State": state}
        if self.reason:
            status["StateChangeReason"] = self.reason
        return {"QueryExecution": {
            "Status": status,
            "Statistics": {"DataScannedInBytes": 778309,
                           "EngineExecutionTimeInMillis": 1087,
                           "TotalExecutionTimeInMillis": 1220}}}

    def get_query_results(self, **_kw):
        page = self.pages[self._page]
        self._page += 1
        return page

    def stop_query_execution(self, QueryExecutionId):  # noqa: N803 - boto3 casing
        self.stopped.append(QueryExecutionId)


def _cursor(fake, timeout_sec=300, started=None):
    cur = athena_exec.AthenaCursor.__new__(athena_exec.AthenaCursor)
    cur._cfg, cur._timeout, cur._request_id = _CFG, timeout_sec, 1
    cur._client = fake
    # Mirrors __init__ deliberately: the constructor is skipped here only to
    # avoid a real boto3 session, so a field added there must be added here or
    # the tests stop exercising the real object.
    cur._on_started = started.append if started is not None else None
    cur.execution_id, cur.stats = None, {}
    cur.description, cur.rowcount, cur._first_page = None, -1, None
    return cur


def _one_page(rows, token=None):
    page = {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": _META}, "Rows": rows}}
    if token:
        page["NextToken"] = token
    return page


def test_the_header_row_is_not_data():
    """Athena repeats the column names as the first row of the first page. Left
    in, every result would carry a row of labels — and a CSV reader has no way
    to tell that row from a real one."""
    fake = _FakeAthena(pages=[_one_page(
        _rows(["user_id", "available"], ["u-1", "9.600000000000000000"]))])
    cur = _cursor(fake)
    cur.execute("SELECT user_id, available FROM t")
    assert list(cur) == [("u-1", "9.600000000000000000")]


def test_a_value_that_happens_to_equal_its_column_name_survives():
    """The header is dropped by matching the metadata, not by dropping row 0
    blindly — otherwise a query whose first row genuinely reads like the header
    would lose it."""
    fake = _FakeAthena(pages=[_one_page(_rows(["u-1", "1.0"], ["u-2", "2.0"]))])
    cur = _cursor(fake)
    cur.execute("SELECT user_id, available FROM t")
    assert list(cur) == [("u-1", "1.0"), ("u-2", "2.0")]


def test_money_stays_a_string():
    """`decimal(38,18)` is 19 significant digits and a float carries 15-16.
    Parsing here would round the tail silently, and the archive's own
    verification is the SUM of these columns."""
    fake = _FakeAthena(pages=[_one_page(
        _rows(["user_id", "available"], ["u-1", "22928969487.129452220000000000"]))])
    cur = _cursor(fake)
    cur.execute("SELECT user_id, available FROM t")
    value = list(cur)[0][1]
    assert isinstance(value, str)
    assert value == "22928969487.129452220000000000"


def test_null_is_none_not_empty_string():
    """Athena omits the key for NULL. An empty string would be written to the
    CSV as a value and read back as one."""
    fake = _FakeAthena(pages=[_one_page(
        _rows(["user_id", "available"], ["u-1", None]))])
    cur = _cursor(fake)
    cur.execute("SELECT user_id, available FROM t")
    assert list(cur) == [("u-1", None)]


def test_the_description_names_the_engines_own_type():
    """The grid's header tooltip reads this. `decimal(38,18)` is the difference
    between a number a reader trusts and one they should not."""
    fake = _FakeAthena(pages=[_one_page(_rows(["user_id", "available"]))])
    cur = _cursor(fake)
    cur.execute("SELECT user_id, available FROM t")
    assert [d[0] for d in cur.description] == ["user_id", "available"]
    assert cur.description[1].type_display == "decimal(38,18)"


def test_paging_follows_the_token():
    fake = _FakeAthena(pages=[
        _one_page(_rows(["user_id", "available"], ["u-1", "1"]), token="n"),
        _one_page(_rows(["u-2", "2"])),
    ])
    cur = _cursor(fake)
    cur.execute("SELECT user_id, available FROM t")
    assert list(cur) == [("u-1", "1"), ("u-2", "2")]


def test_a_failed_query_raises_with_the_services_own_reason():
    """The reason that matters most here is the workgroup's scanned-bytes cap.
    Replacing it with our own wording would hide the one sentence that tells a
    user their query was too expensive rather than wrong."""
    fake = _FakeAthena(states=("FAILED",),
                       reason="Bytes scanned limit was exceeded")
    cur = _cursor(fake)
    with pytest.raises(athena_exec.AthenaQueryError) as e:
        cur.execute("SELECT * FROM t")
    assert "Bytes scanned limit" in str(e.value)


def test_a_timeout_stops_the_query_rather_than_just_giving_up():
    """An abandoned query keeps scanning, and scanning is what this engine
    bills for. Walking away is not the same as stopping."""
    fake = _FakeAthena(states=("RUNNING", "RUNNING", "RUNNING"))
    cur = _cursor(fake, timeout_sec=0)
    with pytest.raises(TimeoutError):
        cur.execute("SELECT * FROM t")
    assert fake.stopped == ["q-1"]


def test_result_reuse_is_never_requested():
    """Athena can return a previous run's result for up to 7 days, and then
    reports `DataScannedInBytes = 0`. The audit trail would record that a query
    scanned nothing when it scanned gigabytes the first time. Agreed with the
    archive side on 2026-09-19: the parameter is never sent."""
    fake = _FakeAthena(pages=[_one_page(_rows(["user_id", "available"]))])
    cur = _cursor(fake)
    cur.execute("SELECT user_id, available FROM t")
    sent = fake.started[0]
    assert "ResultReuseConfiguration" not in sent
    # And no output location either: the workgroup enforces its own, and
    # sending one would put a second copy of the data somewhere ungoverned.
    assert "ResultConfiguration" not in sent
    assert sent["WorkGroup"] == "wg"
    assert sent["QueryExecutionContext"]["Database"] == "archive_db"


def test_the_execution_id_is_announced_before_the_wait():
    """A cancel arriving while the query runs has nothing to aim at but this
    id, and the process that receives the cancel is often not the one running
    the query. So it has to be handed over at the start, not at the end."""
    seen: list[str] = []
    fake = _FakeAthena(pages=[_one_page(_rows(["user_id", "available"]))])
    cur = _cursor(fake, started=seen)
    cur.execute("SELECT user_id, available FROM t")
    assert seen == ["q-1"]


def test_a_failure_to_record_the_id_does_not_lose_the_query():
    """Recording it is best-effort: the query is already running, and throwing
    here would abandon a query that keeps scanning and keeps billing."""
    def _boom(_qid):
        raise RuntimeError("bot database unavailable")
    fake = _FakeAthena(pages=[_one_page(_rows(["user_id", "available"]))])
    cur = _cursor(fake)
    cur._on_started = _boom
    cur.execute("SELECT user_id, available FROM t")
    assert cur.execution_id == "q-1"


def test_the_statistics_are_kept_for_the_audit_trail():
    fake = _FakeAthena(pages=[_one_page(_rows(["user_id", "available"]))])
    cur = _cursor(fake)
    cur.execute("SELECT user_id, available FROM t")
    assert cur.stats["DataScannedInBytes"] == 778309
    assert cur.execution_id == "q-1"
