"""How far the archive reaches, as the approver is told it.

The archive writes one JSON marker: `covered_through` (every row up to here is
in the archive and visible), `computed_at` (when the marker was written, which
proves the pipeline is alive) and an optional `known_gaps`. QueryHub reads it
hourly, stores a verdict, and adds one sentence to the approver's hint.

Nearly every rule below is about one failure: saying "complete" on less
information than it takes. A marker field we cannot read makes the verdict
"unknown", never the soft fallback, because the soft fallback here is the
strongest claim the sentence can make.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from queryhub import athena_exec
from queryhub import config as cfg

_CFG = {"region": "eu-central-1", "workgroup": "wg", "catalog": "AwsDataCatalog",
        "database": "archive_db", "role_arn": "arn:aws:iam::111111111111:role/r",
        "freshness_marker": "s3://example-archive/balance/_meta/ledgers-watermark.json"}

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
_FRESH = "2026-09-30T03:12:00Z"          # 8 h 48 min before _NOW
_DROP = object()                         # a field left out of the marker entirely


def _marker(**fields) -> bytes:
    doc = {"covered_through": "2026-08-25T12:00:00Z", "computed_at": _FRESH}
    doc.update(fields)
    return json.dumps({k: v for k, v in doc.items() if v is not _DROP}).encode()


def _written(hours_ago: float) -> str:
    """A computed_at relative to the real clock, for the paths that read it
    (risk_hint has no `now` to pass)."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ---------------------------------------------------------------------------
# reading the marker: the verdict
# ---------------------------------------------------------------------------

def test_a_well_formed_marker_is_complete():
    v = athena_exec.parse_marker(_marker())
    assert v == {"state": "complete", "covered_through": "2026-08-25T12:00:00Z",
                 "computed_at": "2026-09-30T03:12:00Z", "known_gaps": [],
                 "reason": None}


def test_fields_it_does_not_know_are_ignored():
    """The marker already carries `note` and `table`; the writer must be able to
    add more without a change on this side."""
    v = athena_exec.parse_marker(_marker(
        note="interim marker", table="archive_db.ledgers",
        cutoff_column="message_created_at", rows={"nested": [1, 2, 3]}))
    assert v["state"] == "complete" and v["reason"] is None
    assert set(v) == {"state", "covered_through", "computed_at", "known_gaps", "reason"}


def test_an_offset_is_as_good_as_z_and_is_stored_in_utc():
    v = athena_exec.parse_marker(_marker(covered_through="2026-08-25T15:00:00+03:00",
                                         computed_at="2026-09-30T06:12:00+03:00"))
    assert v["covered_through"] == "2026-08-25T12:00:00Z"
    assert v["computed_at"] == "2026-09-30T03:12:00Z"


@pytest.mark.parametrize("gaps", [[], _DROP], ids=["empty list", "absent"])
def test_no_known_gaps_is_complete(gaps):
    v = athena_exec.parse_marker(_marker(known_gaps=gaps))
    assert v["state"] == "complete" and v["known_gaps"] == []


def test_readable_gaps_make_it_complete_with_gaps():
    v = athena_exec.parse_marker(_marker(known_gaps=[
        {"from": "2026-09-01T13:09:00Z", "to": "2026-09-01T16:10:00+03:00"},
        # A row estimate rode along in the first proposal; extra keys on a gap
        # are ignored like any other unknown field.
        {"from": "2026-09-03T00:00:00Z", "to": "2026-09-03T01:00:00Z",
         "rows_estimate": 26000},
    ]))
    assert v["state"] == "complete_with_gaps"
    assert v["known_gaps"] == [
        {"from": "2026-09-01T13:09:00Z", "to": "2026-09-01T13:10:00Z"},
        {"from": "2026-09-03T00:00:00Z", "to": "2026-09-03T01:00:00Z"}]


# Every way the marker can fail to say how far the archive reaches. Each one is
# "unknown", and none of them may read as complete.
_UNKNOWN = [
    (b"", "not JSON"),
    (b"{\"covered_through\": ", "not JSON"),
    (b"\xff\xfe\x00garbage", "not JSON"),
    (b"[" * 100_000, "not JSON"),                       # nested past the parser's limit
    (b"[]", "not a JSON object"),
    (b"\"2026-08-25T12:00:00Z\"", "not a JSON object"),
    (b"null", "not a JSON object"),
    (_marker(covered_through=_DROP), "covered_through is missing"),
    (_marker(covered_through=None), "covered_through is not a string"),
    (_marker(covered_through=1756123200), "covered_through is not a string"),
    (_marker(covered_through=["2026-08-25T12:00:00Z"]), "covered_through is not a string"),
    (_marker(covered_through="yesterday"), "covered_through is not an ISO-8601 timestamp"),
    (_marker(covered_through="2026-08-25T12:00:00"), "covered_through has no time zone"),
    (_marker(covered_through="2026-08-25"), "covered_through has no time zone"),
    (_marker(covered_through="0001-01-01T00:00:00+01:00"), "covered_through is out of range"),
    (_marker(known_gaps=None), "known_gaps is not a list"),
    (_marker(known_gaps="2026-09-01T13:09Z/2026-09-01T13:10Z"), "known_gaps is not a list"),
    (_marker(known_gaps={"from": "2026-09-01T13:09:00Z", "to": "2026-09-01T13:10:00Z"}),
     "known_gaps is not a list"),
    (_marker(known_gaps=["2026-09-01T13:09:00Z"]), "known_gaps has an item without from and to"),
    (_marker(known_gaps=[{"from": "2026-09-01T13:09:00Z"}]),
     "known_gaps has an item without from and to"),
    (_marker(known_gaps=[{"to": "2026-09-01T13:10:00Z"}]),
     "known_gaps has an item without from and to"),
    (_marker(known_gaps=[{"from": "2026-09-01T13:09:00", "to": "2026-09-01T13:10:00Z"}]),
     "known_gaps has a bound that has no time zone"),
    (_marker(known_gaps=[{"from": "2026-09-01T13:09:00Z", "to": "soon"}]),
     "known_gaps has a bound that is not an ISO-8601 timestamp"),
    (_marker(known_gaps=[{"from": 0, "to": "2026-09-01T13:10:00Z"}]),
     "known_gaps has a bound that is not a string"),
    (_marker(known_gaps=[{"from": "2026-09-01T13:10:00Z", "to": "2026-09-01T13:09:00Z"}]),
     "known_gaps has a gap that ends before it starts"),
    # One unreadable gap poisons the list: listing the others would present a
    # partial list as the whole one.
    (_marker(known_gaps=[{"from": "2026-09-01T13:09:00Z", "to": "2026-09-01T13:10:00Z"},
                         {"from": "2026-09-02"}]),
     "known_gaps has an item without from and to"),
]


@pytest.mark.parametrize("body,reason", _UNKNOWN, ids=[r for _b, r in _UNKNOWN])
def test_what_cannot_be_read_is_unknown(body, reason):
    v = athena_exec.parse_marker(body)
    assert v["state"] == "unknown"
    assert v["reason"] == reason
    # No coverage claim of any kind survives, and "no gaps" is not implied.
    assert v["covered_through"] is None and v["known_gaps"] is None


def test_an_unknown_verdict_keeps_a_readable_computed_at():
    """Coverage and liveness are separate answers: a marker whose gaps cannot
    be read can still say the pipeline stopped writing it days ago."""
    v = athena_exec.parse_marker(_marker(known_gaps=None))
    assert v["state"] == "unknown" and v["computed_at"] == "2026-09-30T03:12:00Z"


@pytest.mark.parametrize("computed_at,reason", [
    (_DROP, "computed_at is missing"),
    ("last night", "computed_at is not an ISO-8601 timestamp"),
    ("2026-09-30T03:12:00", "computed_at has no time zone"),
    (1759201920, "computed_at is not a string"),
])
def test_an_unreadable_computed_at_keeps_the_coverage(computed_at, reason):
    v = athena_exec.parse_marker(_marker(computed_at=computed_at))
    assert v["state"] == "complete"
    assert v["covered_through"] == "2026-08-25T12:00:00Z"
    assert v["computed_at"] is None and v["reason"] == reason


@pytest.mark.parametrize("body", [_marker(), _marker(known_gaps=[
    {"from": "2026-09-01T13:09:00Z", "to": "2026-09-01T13:10:00Z"}]),
    _marker(known_gaps=None), b"not json"])
def test_every_verdict_is_stored_as_it_stands(body):
    v = athena_exec.parse_marker(body)
    assert json.loads(json.dumps(v)) == v


# ---------------------------------------------------------------------------
# the sentence
# ---------------------------------------------------------------------------

def _sentence(verdict, *, stale_hours=36, now=_NOW):
    return athena_exec.freshness_sentence(verdict, stale_hours=stale_hours, now=now)


def test_complete_says_the_date():
    assert _sentence(athena_exec.parse_marker(_marker())) == (
        "Archive complete up to 25 Aug 2026 12:00 UTC.")


def test_complete_with_gaps_lists_them():
    v = athena_exec.parse_marker(_marker(known_gaps=[
        {"from": "2026-09-01T13:09:00Z", "to": "2026-09-01T13:10:00Z"},
        {"from": "2026-09-03T00:00:00Z", "to": "2026-09-03T01:00:00Z"}]))
    assert _sentence(v) == (
        "Archive complete up to 25 Aug 2026 12:00 UTC. Known gaps: "
        "01 Sep 2026 13:09–01 Sep 2026 13:10 UTC, "
        "03 Sep 2026 00:00–03 Sep 2026 01:00 UTC.")


def test_seconds_never_make_the_archive_look_more_complete():
    """Minutes are shown, so seconds go somewhere. Coverage and the start of a
    gap are rounded down, the end of a gap up: every bound shown is one the
    marker actually supports."""
    v = athena_exec.parse_marker(_marker(
        covered_through="2026-08-25T12:00:59.9Z",
        known_gaps=[{"from": "2026-09-01T13:09:40Z", "to": "2026-09-01T13:10:00.207142Z"}]))
    assert _sentence(v) == (
        "Archive complete up to 25 Aug 2026 12:00 UTC. Known gaps: "
        "01 Sep 2026 13:09–01 Sep 2026 13:11 UTC.")


def test_unknown_says_unknown_and_nothing_more():
    """No marker at all: "its time is unknown" would only repeat the sentence."""
    assert _sentence(athena_exec.unknown_verdict("no marker")) == "Archive coverage unknown."


@pytest.mark.parametrize("age,expected", [
    (timedelta(hours=36), ""),                                    # at the limit: not yet
    (timedelta(hours=36, seconds=1), " The freshness marker has not been updated for 36 hours."),
    (timedelta(hours=47, minutes=59), " The freshness marker has not been updated for 47 hours."),
    (timedelta(hours=48), " The freshness marker has not been updated for 2 days."),
    (timedelta(hours=71), " The freshness marker has not been updated for 2 days."),
    (timedelta(days=9, hours=1), " The freshness marker has not been updated for 9 days."),
    (timedelta(hours=-2), ""),                                    # clock skew is not staleness
])
def test_a_marker_older_than_the_limit_says_for_how_long(age, expected):
    v = athena_exec.parse_marker(_marker(computed_at=(_NOW - age).isoformat()))
    assert _sentence(v) == "Archive complete up to 25 Aug 2026 12:00 UTC." + expected


def test_the_limit_is_the_callers():
    v = athena_exec.parse_marker(_marker())           # 8 h 48 min old
    assert _sentence(v, stale_hours=8).endswith("not been updated for 8 hours.")
    assert _sentence(v, stale_hours=24) == "Archive complete up to 25 Aug 2026 12:00 UTC."


def test_one_hour_is_singular():
    v = athena_exec.parse_marker(_marker(computed_at=(_NOW - timedelta(minutes=90)).isoformat()))
    assert _sentence(v, stale_hours=0).endswith("not been updated for 1 hour.")


def test_staleness_is_judged_when_shown_not_when_read():
    """The same stored verdict, shown a week later, says a week."""
    v = athena_exec.parse_marker(_marker())
    assert _sentence(v) == "Archive complete up to 25 Aug 2026 12:00 UTC."
    assert _sentence(v, now=_NOW + timedelta(days=7)).endswith(
        "not been updated for 7 days.")


def test_a_coverage_claim_with_no_marker_time_says_so():
    v = athena_exec.parse_marker(_marker(computed_at=_DROP))
    assert _sentence(v) == ("Archive complete up to 25 Aug 2026 12:00 UTC. "
                            "The freshness marker's time is unknown.")


def test_unknown_coverage_still_reports_a_dead_pipeline():
    v = athena_exec.parse_marker(_marker(known_gaps="?", computed_at="2026-09-20T03:00:00Z"))
    assert _sentence(v) == ("Archive coverage unknown. The freshness marker has "
                            "not been updated for 10 days.")


@pytest.mark.parametrize("verdict", [
    None,
    "complete",
    {},
    {"state": "complete"},
    {"state": "complete", "covered_through": "yesterday"},
    {"state": "complete", "covered_through": "2026-08-25T12:00:00"},
    {"state": "complete", "covered_through": "2026-08-25T12:00:00Z",
     "known_gaps": [{"from": "2026-09-01T13:09:00Z", "to": "2026-09-01T13:10:00Z"}]},
    {"state": "complete_with_gaps", "covered_through": "2026-08-25T12:00:00Z",
     "known_gaps": []},
    {"state": "complete_with_gaps", "covered_through": "2026-08-25T12:00:00Z",
     "known_gaps": [{"from": "2026-09-01T13:09:00Z"}]},
    {"state": "probably", "covered_through": "2026-08-25T12:00:00Z"},
], ids=["none", "a string", "empty", "no date", "bad date", "naive date",
        "complete but with gaps", "gaps but none", "gap without an end",
        "a state it does not know"])
def test_a_stored_verdict_that_does_not_hold_together_reads_as_unknown(verdict):
    """The verdict comes back from a jsonb column anyone with psql can edit."""
    assert _sentence(verdict) == "Archive coverage unknown."


# ---------------------------------------------------------------------------
# the approver's hint
# ---------------------------------------------------------------------------

def _patch_cost(monkeypatch):
    """The scan bound for one named partition, without Glue or S3."""
    monkeypatch.setattr(athena_exec, "_partition_prefixes",
                        lambda c, d, t: ("bucket", "base/events", ["month"]))
    monkeypatch.setattr(athena_exec, "_prefix_bytes",
                        lambda c, b, p: 23_000_000_000)
    monkeypatch.setattr(athena_exec, "_dateless_partition", lambda c, d, t: None)


_COST = ("Scans at most ~23.0 GB (2026-04). "
         "The workgroup cancels any query above its scan limit.")
_SQL = "SELECT a FROM events WHERE month = '2026-04'"


def test_the_hint_appends_the_coverage_to_the_cost(monkeypatch):
    _patch_cost(monkeypatch)
    v = athena_exec.parse_marker(_marker(computed_at=_written(2)))
    assert athena_exec.risk_hint(_CFG, _SQL, freshness=v) == (
        _COST + " Archive complete up to 25 Aug 2026 12:00 UTC.")


def test_without_a_verdict_the_hint_is_what_it_was(monkeypatch):
    _patch_cost(monkeypatch)
    assert athena_exec.risk_hint(_CFG, _SQL) == _COST
    assert athena_exec.risk_hint(_CFG, _SQL, freshness=None) == _COST


def test_the_coverage_stands_alone_when_the_cost_has_nothing_to_say(monkeypatch):
    """A join gets no cost bound, and the hint used to be silent. Coverage is
    still worth a line: "unknown" is an answer an approver needs."""
    _patch_cost(monkeypatch)
    join = "SELECT a FROM x JOIN y ON x.i = y.i"
    assert athena_exec.risk_hint(_CFG, join) is None
    assert athena_exec.risk_hint(_CFG, join, freshness=athena_exec.unknown_verdict(
        "no marker")) == "Archive coverage unknown."


def _setting(monkeypatch, value):
    monkeypatch.setattr(cfg, "get_setting", lambda key, default=None: (
        value if key == "athena_freshness_stale_hours" else default))


def test_the_stale_limit_comes_from_bot_config(monkeypatch):
    _patch_cost(monkeypatch)
    v = athena_exec.parse_marker(_marker(computed_at=_written(9.5)))
    assert athena_exec.risk_hint(_CFG, _SQL, freshness=v) == (
        _COST + " Archive complete up to 25 Aug 2026 12:00 UTC.")        # default 36
    _setting(monkeypatch, "4")
    assert athena_exec.risk_hint(_CFG, _SQL, freshness=v) == (
        _COST + " Archive complete up to 25 Aug 2026 12:00 UTC. "
        "The freshness marker has not been updated for 9 hours.")


def test_an_unreadable_limit_falls_back_to_the_default(monkeypatch):
    _patch_cost(monkeypatch)
    _setting(monkeypatch, "thirty-six")
    v = athena_exec.parse_marker(_marker(computed_at=_written(40.5)))
    assert athena_exec.risk_hint(_CFG, _SQL, freshness=v).endswith(
        "not been updated for 40 hours.")
    fresh = athena_exec.parse_marker(_marker(computed_at=_written(30)))
    assert athena_exec.risk_hint(_CFG, _SQL, freshness=fresh).endswith("12:00 UTC.")


def test_the_hint_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("glue is down")
    monkeypatch.setattr(athena_exec, "scan_estimate", boom)
    monkeypatch.setattr(athena_exec, "freshness_sentence", boom)
    monkeypatch.setattr(cfg, "get_setting", boom)
    # Neither half could be worked out; the one thing it may not do is claim
    # coverage, or take the submission down with it.
    assert athena_exec.risk_hint(_CFG, _SQL, freshness={"state": "complete"}) == (
        "Archive coverage unknown.")
    assert athena_exec.risk_hint(_CFG, _SQL) is None


# ---------------------------------------------------------------------------
# reading S3
# ---------------------------------------------------------------------------

class _ClientError(Exception):
    """The shape of botocore's ClientError, which the suite cannot assume is
    installed (boto3 is the optional `aws` extra)."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class _S3:
    def __init__(self, outcome):
        self.outcome, self.calls, self.bodies = outcome, [], []

    def get_object(self, **kw):
        self.calls.append(kw)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        self.bodies.append(io.BytesIO(self.outcome))
        return {"Body": self.bodies[-1]}


def _session(monkeypatch, outcome):
    s3 = _S3(outcome)
    clients = []

    class _Session:
        def client(self, name, region_name=None):
            clients.append((name, region_name))
            return s3
    monkeypatch.setattr(athena_exec, "session", lambda c: _Session())
    return s3, clients


def test_a_target_with_no_marker_reads_nothing(monkeypatch):
    s3, _ = _session(monkeypatch, _marker())
    no_marker = {k: v for k, v in _CFG.items() if k != "freshness_marker"}
    assert athena_exec.read_freshness(no_marker) is None
    assert athena_exec.read_freshness({**no_marker, "freshness_marker": ""}) is None
    assert s3.calls == []


def test_the_marker_is_read_under_the_targets_role_and_region(monkeypatch):
    s3, clients = _session(monkeypatch, _marker())
    v = athena_exec.read_freshness(_CFG)
    assert clients == [("s3", "eu-central-1")]
    assert s3.calls == [{"Bucket": "example-archive",
                         "Key": "balance/_meta/ledgers-watermark.json"}]
    assert v["state"] == "complete" and v["covered_through"] == "2026-08-25T12:00:00Z"
    read_at = datetime.fromisoformat(v["read_at"])
    assert read_at.tzinfo is not None
    assert abs(datetime.now(timezone.utc) - read_at) < timedelta(minutes=1)
    assert s3.bodies[0].closed          # the connection goes back to the pool


def test_no_object_is_no_marker(monkeypatch):
    _session(monkeypatch, _ClientError("NoSuchKey"))
    v = athena_exec.read_freshness(_CFG)
    assert v["state"] == "unknown" and v["reason"] == "no marker" and "read_at" in v


@pytest.mark.parametrize("error", [_ClientError("AccessDenied"), _ClientError("SlowDown"),
                                   TimeoutError("read timed out"), RuntimeError("sts")])
def test_any_other_failure_is_unreadable(monkeypatch, error):
    _session(monkeypatch, error)
    v = athena_exec.read_freshness(_CFG)
    assert v["state"] == "unknown" and v["reason"] == "unreadable"


def test_a_failed_assume_role_is_unreadable_not_an_exception(monkeypatch):
    def no_session(c):
        raise _ClientError("AccessDenied")
    monkeypatch.setattr(athena_exec, "session", no_session)
    assert athena_exec.read_freshness(_CFG)["reason"] == "unreadable"


def test_the_real_botocore_error_is_recognised(monkeypatch):
    exceptions = pytest.importorskip("botocore.exceptions")
    _session(monkeypatch, exceptions.ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}},
        "GetObject"))
    assert athena_exec.read_freshness(_CFG)["reason"] == "no marker"


@pytest.mark.parametrize("uri", ["example-archive/balance/marker.json",
                                 "https://example-archive.s3.amazonaws.com/m.json",
                                 "s3://example-archive", "s3:///balance/marker.json"])
def test_a_marker_uri_that_is_not_s3_bucket_and_key_is_unreadable(monkeypatch, uri):
    s3, _ = _session(monkeypatch, _marker())
    v = athena_exec.read_freshness({**_CFG, "freshness_marker": uri})
    assert v["state"] == "unknown" and v["reason"] == "unreadable"
    assert s3.calls == []


def test_an_object_too_large_to_be_a_marker_is_not_parsed(monkeypatch):
    _session(monkeypatch, b" " * (64 * 1024) + _marker())
    v = athena_exec.read_freshness(_CFG)
    assert v["state"] == "unknown" and v["reason"] == "too large to be a marker"


def test_a_broken_marker_is_read_as_unknown_not_raised(monkeypatch):
    _session(monkeypatch, b"<html>AccessDenied</html>")
    v = athena_exec.read_freshness(_CFG)
    assert v["state"] == "unknown" and v["reason"] == "not JSON"
