"""Say WHEN the access expired, and say it in a form a client can draw.

`effective_grant_for_user` returns None for two situations that read
identically to the person refused: never had access, and had it until
Thursday. The second is the whole point of letting grants expire, and it is
also the one that produces "it worked last week" as a support question.

The date is carried as a FIELD, not only in the sentence. This codebase has
already been bitten once by a client parsing prose — the duplicate wording
stopped matching the client's regex and the mismatch was live — so a refusal
that a UI is meant to branch on gets its own code and its own key.
"""
from datetime import datetime, timezone

from queryhub import core_submit, teams
from queryhub.web import routes_queries

AUG14 = datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc)


# --- the lookup -------------------------------------------------------------

def test_a_lapsed_grant_returns_its_datetime(monkeypatch):
    monkeypatch.setattr(teams.db, "fetch_one", lambda *a, **k: {"at": AUG14})
    assert teams.expired_grant_at("U0AB12CD34", 2) == AUG14


def test_never_having_had_access_returns_nothing(monkeypatch):
    """Which is what keeps the generic refusal for the generic case."""
    monkeypatch.setattr(teams.db, "fetch_one", lambda *a, **k: {"at": None})
    assert teams.expired_grant_at("U0AB12CD34", 2) is None


def test_no_row_at_all_returns_nothing(monkeypatch):
    monkeypatch.setattr(teams.db, "fetch_one", lambda *a, **k: None)
    assert teams.expired_grant_at("U0AB12CD34", 2) is None


# --- the wording ------------------------------------------------------------

def test_the_human_form_reads_as_a_date():
    assert teams.fmt_lapsed(AUG14) == "14 Aug 2026"


def test_a_single_digit_day_has_no_padding():
    """'4 Aug' reads as a date; '04 Aug' reads as a serial number."""
    assert teams.fmt_lapsed(datetime(2026, 8, 4, tzinfo=timezone.utc)) == "4 Aug 2026"


# --- the rejection ----------------------------------------------------------

def test_the_refusal_has_its_own_reason_and_carries_the_date(monkeypatch):
    monkeypatch.setattr(core_submit.teams, "effective_grant_for_user",
                        lambda p, t: None)
    monkeypatch.setattr(core_submit.teams, "expired_grant_at",
                        lambda p, t: AUG14)
    rej = core_submit.Rejection(
        "server",
        f"Your access to this server expired on {teams.fmt_lapsed(AUG14)}. "
        "Ask an admin to extend it.",
        reason="access_expired",
        detail={"expiredOn": AUG14.date().isoformat()})
    assert rej.reason == "access_expired"
    assert rej.detail == {"expiredOn": "2026-08-14"}
    assert "14 Aug 2026" in rej.message


def test_the_reason_maps_to_its_own_http_code():
    """Not the `forbidden` it would otherwise share with 'never allowed here'."""
    status, code = routes_queries._REJECTION_HTTP["access_expired"]
    assert (status, code) == (403, "access_expired")
    assert routes_queries._REJECTION_HTTP["server"] == (403, "forbidden")
    assert code != "forbidden", "the client cannot branch on a shared code"


def test_the_envelope_forwards_the_detail(monkeypatch):
    """`expiredOn` has to survive the trip, or the UI is back to regexing."""
    captured = {}

    def fake_error(status, code, message, **extra):
        captured.update(status=status, code=code, message=message, extra=extra)
        return RuntimeError("stop")
    monkeypatch.setattr(routes_queries.deps, "_error", fake_error)

    rej = core_submit.Rejection(
        "server", "Your access to this server expired on 14 Aug 2026.",
        reason="access_expired", detail={"expiredOn": "2026-08-14"})
    try:
        routes_queries._reject(rej)
    except RuntimeError:
        pass
    assert captured["code"] == "access_expired"
    assert captured["extra"]["expiredOn"] == "2026-08-14"


def test_a_rejection_without_detail_keeps_the_old_envelope(monkeypatch):
    """Additive: every existing rejection produces the same body it always did."""
    captured = {}

    def fake_error(status, code, message, **extra):
        captured.update(code=code, extra=extra)
        return RuntimeError("stop")
    monkeypatch.setattr(routes_queries.deps, "_error", fake_error)
    try:
        routes_queries._reject(core_submit.Rejection("server", "Not authorized."))
    except RuntimeError:
        pass
    assert captured["code"] == "forbidden"
    assert "expiredOn" not in captured["extra"]
