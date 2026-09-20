"""The number an approver sees before saying yes.

Athena bills for bytes scanned and will not tell you how many before it runs:
no dry run, and its EXPLAIN returns a shape rather than a size. So the approver
gets an upper bound built from the objects the query could read — and the tests
here are mostly about the ways such a bound can be worse than useless.

The bound is deliberately NOT built from the catalog's statistics. Measured on
the real archive 2026-09-20: those table parameters were five weeks stale and
understated it by 8x (93 GB against 770 GB). Wrong in the reassuring direction
is the failure that gets a screen ignored.
"""
from queryhub import athena_exec

_CFG = {"region": "eu-central-1", "workgroup": "wg", "catalog": "AwsDataCatalog",
        "database": "archive_db", "role_arn": "arn:aws:iam::111111111111:role/r"}


def _patch(monkeypatch, sizes, part_keys=("month",), partitions=()):
    monkeypatch.setattr(athena_exec, "_partition_prefixes",
                        lambda cfg, db, t: ("bucket", "base/events",
                                            list(part_keys)))
    monkeypatch.setattr(athena_exec, "_prefix_bytes",
                        lambda cfg, bucket, prefix: sizes.get(prefix, 0))
    monkeypatch.setattr(athena_exec, "_dateless_partition",
                        lambda cfg, db, t: ("month=unknown"
                                            if "unknown" in partitions else None))


_SIZES = {
    "base/events": 769_000_000_000,
    "base/events/month=2026-04": 23_000_000_000,
    "base/events/month=2026-08": 363_000_000_000,
    "base/events/month=unknown": 650_000_000,
}


def test_a_named_partition_bounds_the_query_to_that_partition(monkeypatch):
    _patch(monkeypatch, _SIZES)
    est = athena_exec.scan_estimate(_CFG, "SELECT a FROM events WHERE month = '2026-04'")
    assert est["partitions"] == ["2026-04"]
    assert est["bytes"] == 23_000_000_000


def test_several_named_partitions_add_up(monkeypatch):
    _patch(monkeypatch, _SIZES)
    est = athena_exec.scan_estimate(
        _CFG, "SELECT a FROM events WHERE month IN ('2026-04','2026-08')")
    assert est["bytes"] == 386_000_000_000


def test_no_partition_filter_means_the_whole_table(monkeypatch):
    _patch(monkeypatch, _SIZES)
    est = athena_exec.scan_estimate(_CFG, "SELECT a FROM events WHERE user_id = 'x'")
    assert est["partitions"] == []
    assert est["bytes"] == 769_000_000_000


def test_a_range_on_the_partition_key_pins_nothing(monkeypatch):
    """`month >= '2026-07'` names no literal partition, and guessing which ones
    it covers would be arithmetic on a string. Falling back to the whole table
    is wrong in the expensive direction, which is the only safe direction."""
    _patch(monkeypatch, _SIZES)
    est = athena_exec.scan_estimate(_CFG, "SELECT a FROM events WHERE month >= '2026-07'")
    assert est["bytes"] == 769_000_000_000


def test_a_count_reads_footers_and_the_bound_says_so(monkeypatch):
    """Measured on the real archive: `count(*)` over 24 billion rows scanned
    0.00 MB. A bound that said "at most 770 GB" here would be true, useless,
    and the fastest way to teach an approver to skip the line."""
    _patch(monkeypatch, _SIZES)
    est = athena_exec.scan_estimate(_CFG, "SELECT count(*) FROM events")
    assert est["metadata_only"] is True
    assert est["bytes"] == 0


def test_grouping_by_the_partition_column_is_still_metadata_only(monkeypatch):
    _patch(monkeypatch, _SIZES)
    est = athena_exec.scan_estimate(
        _CFG, "SELECT month, count(*) FROM events GROUP BY month")
    assert est["metadata_only"] is True


def test_a_join_gets_no_bound_rather_than_a_guessed_one(monkeypatch):
    _patch(monkeypatch, _SIZES)
    est = athena_exec.scan_estimate(
        _CFG, "SELECT a FROM events e JOIN other o ON o.id = e.id")
    assert est["bytes"] is None      # unknown, reported as unknown


def test_unparseable_sql_yields_no_bound(monkeypatch):
    _patch(monkeypatch, _SIZES)
    assert athena_exec.scan_estimate(_CFG, "NOT SQL AT ALL ((")["bytes"] is None


def test_the_hint_warns_that_dateless_rows_are_excluded(monkeypatch):
    """A query narrowed by date cannot match rows that have no date — they sit
    in their own partition and are silently absent. The warning belongs where
    the user is about to be misled, not in a note beside the connection."""
    _patch(monkeypatch, _SIZES, partitions=("unknown",))
    hint = athena_exec.risk_hint(_CFG, "SELECT a FROM events WHERE month = '2026-04'")
    assert "23.0 GB" in hint
    assert "month=unknown" in hint and "NOT included" in hint


def test_the_hint_does_not_warn_when_those_rows_are_asked_for(monkeypatch):
    _patch(monkeypatch, _SIZES, partitions=("unknown",))
    hint = athena_exec.risk_hint(
        _CFG, "SELECT a FROM events WHERE month IN ('2026-04','unknown')")
    assert "NOT included" not in hint


def test_the_hint_is_silent_when_it_has_nothing_to_say(monkeypatch):
    """No bound, no line. A hint nobody can act on teaches people to skip the
    ones that matter."""
    _patch(monkeypatch, _SIZES)
    assert athena_exec.risk_hint(_CFG, "SELECT a FROM x JOIN y ON x.i = y.i") is None
