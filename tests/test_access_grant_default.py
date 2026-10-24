"""A grant that an access-request approval writes ends in 90 days by default.

Operator decision, 2026-10-24 (K4, K4b):

  * The new setting `access_grant_default_days` (default 90, from 1 to 365).
  * The web approval takes an optional `expiresAt`, or `noEnd: true`. An admin
    can still give a grant with no end. Only the DEFAULT is 90 days.
  * The Slack Approve button takes the default.
  * Grants that exist now keep their end dates. An approval that merges into an
    active grant of the same tier keeps that grant's end.

The tests use fakes: no database, no Slack.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from queryhub import access_grant_default as agd
from queryhub import access_requests as ar
from queryhub import config as cfg
from queryhub.slack_app import handlers
from queryhub.web import config_admin
from queryhub.web import routes_admin as ra

DAY = timedelta(days=1)
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def _setting(monkeypatch, value, key="access_grant_default_days"):
    real = cfg.get_setting
    monkeypatch.setattr(cfg, "get_setting",
                        lambda k, d=None: value if k == key else real(k, d))


# ---- the setting ----------------------------------------------------------------

def test_the_default_is_90_days():
    assert agd.default_days() == 90


@pytest.mark.parametrize("raw,days", [("1", 1), ("30", 30), ("365", 365)])
def test_a_stored_value_in_range_is_used(monkeypatch, raw, days):
    _setting(monkeypatch, raw)
    assert agd.default_days() == days


@pytest.mark.parametrize("raw", ["0", "", "-5", "366", "abc", "30.5"])
def test_a_bad_stored_value_counts_as_90(monkeypatch, raw):
    _setting(monkeypatch, raw)
    assert agd.default_days() == 90


def test_an_unreadable_setting_counts_as_90_never_as_no_end(monkeypatch):
    def boom(k, d=None):
        raise RuntimeError("bot_config is unreachable")
    monkeypatch.setattr(cfg, "get_setting", boom)
    assert agd.default_days() == 90
    assert agd.approval_end(now=NOW) == NOW + 90 * DAY


# ---- approval_end: the one rule ---------------------------------------------------

def test_no_choice_gives_now_plus_the_default():
    assert agd.approval_end(now=NOW, days=90) == NOW + 90 * DAY


def test_the_default_follows_the_setting(monkeypatch):
    _setting(monkeypatch, "45")
    assert agd.approval_end(now=NOW) == NOW + 45 * DAY


def test_an_explicit_end_wins():
    end = NOW + 10 * DAY
    assert agd.approval_end(end, now=NOW, days=90) == end
    assert agd.approval_end(end.isoformat(), now=NOW, days=90) == end
    assert agd.approval_end("2026-11-03T12:00:00Z", now=NOW, days=90) == end


def test_an_explicit_end_has_no_upper_limit():
    far = NOW + 2000 * DAY
    assert agd.approval_end(far, now=NOW, days=90) == far


def test_no_end_wins_and_means_none():
    assert agd.approval_end(no_end=True, now=NOW, days=90) is None


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_end_is_no_choice(blank):
    assert agd.approval_end(blank, now=NOW, days=90) == NOW + 90 * DAY


def test_no_end_together_with_an_end_is_refused():
    with pytest.raises(agd.EndRefused) as e:
        agd.approval_end(NOW + DAY, no_end=True, now=NOW)
    assert e.value.code == "bad_request"


@pytest.mark.parametrize("past", [NOW, NOW - DAY, "2020-01-01T00:00:00Z"])
def test_an_end_in_the_past_is_refused(past):
    with pytest.raises(agd.EndRefused) as e:
        agd.approval_end(past, now=NOW)
    assert e.value.code == "end_in_past"


def test_a_string_that_is_not_a_time_is_refused():
    with pytest.raises(agd.EndRefused) as e:
        agd.approval_end("next week", now=NOW)
    assert e.value.code == "bad_request"


def test_a_naive_time_is_read_as_utc():
    assert agd.approval_end("2026-11-03T12:00:00", now=NOW) == NOW + 10 * DAY


# ---- decide(): the grant that is written ------------------------------------------

@pytest.fixture(autouse=True)
def _ordinary_target(monkeypatch):
    monkeypatch.setattr("queryhub.grants.control_plane_target_ids", lambda: set())


class FakeConn:
    def __init__(self, fetch_results):
        self.executed = []
        self._fetches = list(fetch_results)

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return self._fetches.pop(0)

    def insert_params(self):
        hits = [p for s, p in self.executed if "INSERT INTO user_target_grants" in s]
        assert len(hits) == 1, hits
        return hits[0]


ROW = {
    "id": 13, "requester_slack_id": "U0EXAMPLE01", "requester_name": "Dev One",
    "target_server_id": 21, "database_name": "shipping_service",
    "attempted_query": None, "reason": "delivery debugging",
    "requested_tier": "ro", "status": "approved",
    "decided_by_slack_id": "U0EXAMPLE99", "decided_by_name": "admin",
    "decision_reason": None, "created_at": None, "decided_at": None,
}


def _grant(mode="ro", expires_at=None, active=True):
    return {"mode": mode, "allowed_databases": ["other_db"],
            "expires_at": expires_at, "active": active}


@pytest.fixture
def audit_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(ar.audit, "log_in",
                        lambda cur, rid, aid, aname, action, details=None:
                        calls.append((action, details)))
    return calls


def _wire(monkeypatch, conn):
    @contextmanager
    def fake_txn():
        yield conn
    monkeypatch.setattr(ar.db, "transaction", fake_txn)


def _near(end, days):
    return abs(end - (datetime.now(timezone.utc) + timedelta(days=days))) < timedelta(minutes=1)


def test_approve_with_no_choice_writes_the_default_end(monkeypatch, audit_calls):
    conn = FakeConn([dict(ROW), None])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    end = out["auto_grant"]["expires_at"]
    assert _near(end, 90)
    assert conn.insert_params()[-1] == end
    assert audit_calls[0][1]["end_source"] == "default"


def test_the_default_days_setting_changes_the_grant_end(monkeypatch, audit_calls):
    _setting(monkeypatch, "30")
    conn = FakeConn([dict(ROW), None])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert _near(out["auto_grant"]["expires_at"], 30)


def test_an_explicit_end_wins_over_the_default(monkeypatch, audit_calls):
    end = datetime.now(timezone.utc) + 200 * DAY
    conn = FakeConn([dict(ROW), None])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None, expires_at=end.isoformat())
    assert out["auto_grant"]["expires_at"] == end
    assert conn.insert_params()[-1] == end
    assert audit_calls[0][1]["end_source"] == "chosen"


def test_no_end_wins_over_the_default(monkeypatch, audit_calls):
    conn = FakeConn([dict(ROW), None])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None, no_end=True)
    assert out["auto_grant"]["applied"] is True
    assert out["auto_grant"]["expires_at"] is None
    assert conn.insert_params()[-1] is None
    assert audit_calls[0][1]["expires_at"] is None
    assert audit_calls[0][1]["end_source"] == "no_end"


def test_a_bad_end_is_refused_before_anything_is_written(monkeypatch, audit_calls):
    conn = FakeConn([dict(ROW), None])
    _wire(monkeypatch, conn)
    with pytest.raises(agd.EndRefused):
        ar.decide(13, "approved", "U0EXAMPLE99", "admin", None,
                  expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(agd.EndRefused):
        ar.decide(13, "approved", "U0EXAMPLE99", "admin", None,
                  expires_at=(datetime.now(timezone.utc) + DAY).isoformat(), no_end=True)
    assert conn.executed == [] and audit_calls == []


def test_a_merge_into_an_active_grant_keeps_its_end(monkeypatch, audit_calls):
    kept = datetime.now(timezone.utc) + 4 * DAY
    conn = FakeConn([dict(ROW), _grant(expires_at=kept)])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert out["auto_grant"]["expires_at"] == kept
    assert audit_calls[0][1]["end_source"] == "kept"


def test_a_merge_into_an_active_grant_with_no_end_leaves_it_without_one(monkeypatch, audit_calls):
    """Existing grants are not touched, an old open-ended one included."""
    conn = FakeConn([dict(ROW), _grant(expires_at=None)])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert out["auto_grant"]["expires_at"] is None


def test_a_lapsed_grant_gets_a_fresh_default_end(monkeypatch, audit_calls):
    past = datetime.now(timezone.utc) - 3 * DAY
    conn = FakeConn([dict(ROW), _grant(expires_at=past, active=False)])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "approved", "U0EXAMPLE99", "admin", None)
    assert _near(out["auto_grant"]["expires_at"], 90)


def test_a_reject_ignores_an_end(monkeypatch, audit_calls):
    conn = FakeConn([dict(ROW, status="rejected")])
    _wire(monkeypatch, conn)
    out = ar.decide(13, "rejected", "U0EXAMPLE99", "admin", "no",
                    expires_at="garbage", no_end=True)
    assert "auto_grant" not in out
    assert not [s for s, _ in conn.executed if "user_target_grants" in s]


# ---- the Slack button takes the default -------------------------------------------

def test_the_slack_approve_button_sends_no_end(monkeypatch):
    seen = {}

    def fake_decide(*a, **kw):
        seen["a"], seen["kw"] = a, kw
        return None   # "already decided": the handler stops after the DM
    monkeypatch.setattr(handlers, "_guard_admin", lambda *a, **k: True)
    monkeypatch.setattr(handlers, "_admin_in_scope", lambda *a, **k: True)
    monkeypatch.setattr(handlers.access_requests, "get",
                        lambda rid: dict(ROW, status="pending"))
    monkeypatch.setattr(handlers.access_requests, "decide", fake_decide)
    monkeypatch.setattr(handlers.notifications, "dm_requester", lambda *a, **k: None)
    body = {"user": {"id": "U0EXAMPLE99", "name": "admin"}, "actions": [{"value": "13"}]}
    handlers.handle_access_approve(MagicMock(), body, MagicMock())
    assert seen["a"][:2] == (13, "approved")
    assert "expires_at" not in seen["kw"] and "no_end" not in seen["kw"], seen
    # So `decide` takes the default for it, as the tests above show.


# ---- the web route ----------------------------------------------------------------

CLAIMS = {"sub": "U0ADMINAAAA", "name": "Admin One"}
PENDING = {"id": 17, "requester_slack_id": "U0REQUESTER", "requester_name": "dev.one",
           "target_server_id": 52, "database_name": None, "requested_tier": None,
           "attempted_query": None, "reason": "need one look", "status": "pending",
           "created_at": None, "decided_at": None}


@pytest.fixture
def web(monkeypatch):
    st = SimpleNamespace(kw=None, end=NOW + 90 * DAY, refuse=None)
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need, **kw: claims["sub"])
    monkeypatch.setattr(ra, "_alias_of", lambda tid: "demo-orders")
    monkeypatch.setattr(ra.access_requests, "get", lambda rid: dict(PENDING) if rid == 17 else None)
    monkeypatch.setattr(ra, "_announce_endpoint_decision", lambda *a, **k: None)

    @contextmanager
    def fake_txn():
        yield object()
    monkeypatch.setattr(ra.db, "transaction", fake_txn)
    monkeypatch.setattr(ra.audit, "log_in", lambda *a, **k: None)

    def fake_decide(rid, status, uid, name, note, **kw):
        st.kw = kw
        if st.refuse:
            raise st.refuse
        row = dict(PENDING, status=status)
        if status == "approved":
            row["auto_grant"] = {"applied": True, "reason": "granted", "mode": "ro",
                                 "databases": None, "expires_at": st.end}
        return row
    monkeypatch.setattr(ra.access_requests, "decide", fake_decide)
    return st


def _approve(**body):
    return ra.admin_decide_endpoint(17, ra.EndpointDecisionIn(approve=True, **body), CLAIMS)


def test_the_route_passes_no_choice_and_answers_the_end(web):
    out = _approve()
    assert web.kw == {"expires_at": None, "no_end": False}
    assert out["expiresAt"] == web.end.isoformat()


def test_the_route_passes_an_explicit_end(web):
    _approve(expiresAt="2027-03-01T00:00:00Z")
    assert web.kw == {"expires_at": "2027-03-01T00:00:00Z", "no_end": False}


def test_the_route_passes_no_end_and_answers_null(web):
    web.end = None
    out = _approve(noEnd=True)
    assert web.kw == {"expires_at": None, "no_end": True}
    assert "expiresAt" in out and out["expiresAt"] is None


@pytest.mark.parametrize("code", ["end_in_past", "bad_request"])
def test_a_refused_end_is_a_400_with_its_code(web, code):
    web.refuse = agd.EndRefused(code, "The end date is in the past. Pick a later date.")
    with pytest.raises(HTTPException) as e:
        _approve(expiresAt="2020-01-01T00:00:00Z")
    assert e.value.status_code == 400
    assert e.value.detail["code"] == code


def test_a_reject_sends_no_end_even_if_the_body_has_one(web):
    out = ra.admin_decide_endpoint(
        17, ra.EndpointDecisionIn(approve=False, expiresAt="2027-03-01T00:00:00Z", noEnd=True),
        CLAIMS)
    assert web.kw == {"expires_at": None, "no_end": False}
    assert "expiresAt" not in out


def test_the_list_carries_the_default_days(monkeypatch):
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need, **kw: claims["sub"])
    monkeypatch.setattr(ra.db, "fetch_all", lambda sql, params=None: [])
    monkeypatch.setattr(ra.people, "namer", lambda names: (lambda n: n))
    assert ra.admin_endpoint_requests(claims=CLAIMS) == {"requests": [], "accessDefaultDays": 90}
    _setting(monkeypatch, "30")
    assert ra.admin_endpoint_requests(claims=CLAIMS)["accessDefaultDays"] == 30


# ---- the System configuration screen ----------------------------------------------

KEY = "access_grant_default_days"


class ConfigCur:
    def __init__(self, value="90"):
        self.value = value
        self.updates = []

    def execute(self, sql, params=None):
        if sql.startswith("UPDATE"):
            self.updates.append(params)

    def fetchall(self):
        return [{"key": KEY, "value": self.value}]


@pytest.mark.parametrize("raw", [0, "", "  ", -1, 366, "abc"])
def test_the_screen_refuses_zero_blank_negative_and_above_365(raw):
    cur = ConfigCur()
    with pytest.raises(ValueError, match="from 1 to 365"):
        config_admin.apply_config({KEY: raw}, cur)
    assert cur.updates == []


@pytest.mark.parametrize("raw,stored", [(1, "1"), ("30", "30"), (365, "365")])
def test_the_screen_accepts_a_whole_number_from_1_to_365(raw, stored):
    cur = ConfigCur()
    assert config_admin.apply_config({KEY: raw}, cur) == [{"key": KEY, "from": "90", "to": stored}]
    assert cur.updates == [(stored, KEY)]


def test_the_key_is_a_number_field_in_the_grants_group():
    assert config_admin._TYPES[KEY] == "int"
    assert config_admin._RANGES[KEY] == (1, 365)
    groups = {key: keys for key, _title, keys in config_admin._GROUPS}
    assert KEY in groups["grants"]


# ---- the migration ----------------------------------------------------------------

MIG = Path(__file__).resolve().parents[1] / "migrations" / "194_access_grant_default_days.sql"


def test_the_migration_seeds_the_key_once_and_keeps_a_hand_set_value():
    sql = MIG.read_text()
    assert "INSERT INTO bot_config" in sql
    assert "'access_grant_default_days', '90'" in sql
    assert "ON CONFLICT (key) DO NOTHING" in sql
    assert "UPDATE" not in sql.replace("-- ", "").split("INSERT INTO", 1)[1]
