"""Noticing that a target's schema catalog stopped refreshing.

The hourly refresh logged a warning on failure and exited 0, so a target whose
catalog went stale was invisible: browse, search and the `/sql` autocomplete
kept serving the last good snapshot and nothing said it was old. The first
symptom was somebody asking why a table they had just created was missing.

Two decisions carry the feature, and both are here because both have an
obvious wrong version:

  * **what counts as a failed refresh.** Alerting whenever any database failed
    would page forever for one permanently-broken database on an otherwise
    healthy target — an alert people learn to filter, which is worse than the
    gap it reports.
  * **when to say it.** An hourly job with no memory sends an hourly DM for as
    long as the outage lasts.
"""
from __future__ import annotations

import pytest

from queryhub import schema_catalog


# ---------------------------------------------------------------------------
# what counts as failed
# ---------------------------------------------------------------------------

def test_a_normal_run_is_ok():
    ok, err = schema_catalog.summary_is_ok({"app": "12 tables / 40 columns (0.2s)"})
    assert ok is True and err is None


def test_one_broken_database_out_of_several_is_still_ok():
    """The catalog is mostly fresh, so this is not an outage. The error is
    still reported so the log names it."""
    ok, err = schema_catalog.summary_is_ok({
        "app": "12 tables / 40 columns (0.2s)",
        "log": "failed: OperationalError: boom",
    })
    assert ok is True
    assert "log" in err


@pytest.mark.parametrize("summary,why", [
    ({"app": "failed: OperationalError: boom"}, "every database failed"),
    ({"*": "unreachable: OperationalError"}, "the target is unreachable"),
    ({"*": "skipped: no RO credential"}, "an enabled target with no credential"),
    ({}, "nothing was even enumerated"),
])
def test_a_target_with_nothing_snapshotted_has_failed(summary, why):
    ok, err = schema_catalog.summary_is_ok(summary)
    assert ok is False, why
    assert err, "a failure must carry something to read"


# ---------------------------------------------------------------------------
# when to say it
# ---------------------------------------------------------------------------

def test_nothing_is_said_before_the_threshold():
    for failures in (1, 2):
        alert, recovered = schema_catalog.refresh_verdict(
            ok=False, failures=failures, outstanding=False, alert_after=3)
        assert not alert and not recovered


def test_the_alert_fires_on_the_run_that_crosses_the_threshold():
    alert, recovered = schema_catalog.refresh_verdict(
        ok=False, failures=3, outstanding=False, alert_after=3)
    assert alert is True and recovered is False


def test_it_does_not_fire_again_while_the_outage_lasts():
    """The bug this prevents: an hourly DM for a day-long outage."""
    for failures in (4, 5, 99):
        alert, _ = schema_catalog.refresh_verdict(
            ok=False, failures=failures, outstanding=True, alert_after=3)
        assert alert is False


def test_recovery_is_announced_once():
    alert, recovered = schema_catalog.refresh_verdict(
        ok=True, failures=0, outstanding=True, alert_after=3)
    assert recovered is True and alert is False


def test_a_success_with_no_alert_outstanding_says_nothing():
    """Every healthy run of every healthy target goes through here. If this
    ever returned True the fleet would DM the admins hourly, 44 times."""
    alert, recovered = schema_catalog.refresh_verdict(
        ok=True, failures=0, outstanding=False, alert_after=3)
    assert not alert and not recovered


def test_a_threshold_of_one_alerts_immediately():
    alert, _ = schema_catalog.refresh_verdict(
        ok=False, failures=1, outstanding=False, alert_after=1)
    assert alert is True
