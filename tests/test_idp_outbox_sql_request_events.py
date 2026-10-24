"""Outbox events of a SQL request that is decided, withdrawn or expired.

`idp_outbox` writes three events: queryhub.request_decided,
queryhub.request_withdrawn and queryhub.request_expired. The database is a
recorder. The call sites are checked in the source.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from queryhub import cancellation, core_decide, db, idp_outbox, targets
from queryhub import config as cfg

REQUESTER = "U0EXAMPLE001"
SQL_TEXT = "SELECT secret_column FROM payroll"
TARGET = targets.TargetServer(
    id=21, alias="demo-orders", host="h.example", port=5432,
    default_database="demo_db", username="u", enabled=True, notes=None,
    engine="postgres")
ROW = {"id": 41, "requester_slack_id": REQUESTER, "target_server_id": 21,
       "required_tier": "ro", "decided_by_slack_id": "U0EXAMPLE002",
       "decided_by_name": "Jordan Ray", "decision_reason": "too broad",
       "query": SQL_TEXT, "attempted_query": SQL_TEXT}


class World:
    def __init__(self):
        self.flag = "on"
        self.fail_insert = False
        self.rows: list = []


@pytest.fixture
def w(monkeypatch):
    w = World()
    real = cfg.get_setting
    monkeypatch.setattr(
        cfg, "get_setting",
        lambda key, default=None: w.flag if key == "idp_outbox_enabled"
        else real(key, default))

    def execute(sql, params=None):
        if w.fail_insert:
            raise RuntimeError("the outbox insert failed")
        w.rows.append((" ".join(sql.split()), params))
    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(db, "fetch_one", lambda sql, params=None: dict(ROW))
    monkeypatch.setattr(targets, "get", lambda tid: TARGET if tid == 21 else None)
    return w


def _payload(w, i=0) -> dict:
    return json.loads(w.rows[i][1][3])


@pytest.mark.parametrize("decision,word", [
    ("approve", "approved"), ("reject", "rejected"), ("changes", "changes_requested")])
def test_decided_writes_one_row_with_the_decision_word(w, decision, word):
    idp_outbox.write_request_decided(dict(ROW), decision)

    assert len(w.rows) == 1
    sql, (event, request_id, recipients, _p) = w.rows[0]
    assert "VALUES (%s, %s, %s, %s)" in sql
    assert event == "queryhub.request_decided"
    assert request_id == 41 and recipients == [REQUESTER]
    assert _payload(w) == {
        "requestId": "41", "decision": word, "target": "demo-orders", "tier": "ro",
        "decidedBy": "Jordan Ray", "note": "too broad"}


def test_withdrawn_writes_one_row(w):
    idp_outbox.write_request_withdrawn(41, "Dev One")

    assert len(w.rows) == 1
    _sql, (event, request_id, recipients, _p) = w.rows[0]
    assert (event, request_id, recipients) == (
        "queryhub.request_withdrawn", 41, [REQUESTER])
    assert _payload(w) == {"requestId": "41", "target": "demo-orders", "tier": "ro",
                           "decidedBy": "Dev One", "note": ""}


def test_expired_writes_one_row(w):
    idp_outbox.write_request_expired(41, REQUESTER, "demo-orders", "rw",
                                     note="expired: no admin action within 24h")

    assert len(w.rows) == 1
    _sql, (event, request_id, recipients, _p) = w.rows[0]
    assert (event, request_id, recipients) == (
        "queryhub.request_expired", 41, [REQUESTER])
    assert _payload(w)["note"] == "expired: no admin action within 24h"
    assert _payload(w)["tier"] == "rw"


def _write_all():
    idp_outbox.write_request_decided(dict(ROW), "approve")
    idp_outbox.write_request_withdrawn(41, "Dev One")
    idp_outbox.write_request_expired(41, REQUESTER, "demo-orders", "ro")


def test_every_payload_value_is_a_string_even_for_a_sparse_row(w):
    sparse = {"id": 5, "requester_slack_id": REQUESTER}
    idp_outbox.write_request_decided(sparse, "reject")
    idp_outbox.write_request_expired(5, REQUESTER, None, None)
    _write_all()

    assert len(w.rows) == 5
    for i in range(5):
        payload = _payload(w, i)
        assert payload and all(isinstance(v, str) for v in payload.values()), payload


def test_no_payload_holds_the_sql_text(w):
    _write_all()
    assert all(SQL_TEXT not in params[3] and "payroll" not in params[3]
               for _sql, params in w.rows)


def test_nothing_is_written_while_the_switch_is_off(w):
    w.flag = "off"
    _write_all()
    assert w.rows == []


def test_a_failing_insert_is_logged_and_never_raised(w, caplog):
    w.fail_insert = True

    _write_all()

    assert w.rows == []
    for event in ("request_decided", "request_withdrawn", "request_expired"):
        assert f"failed to write the queryhub.{event} notification_outbox row" in caplog.text


# ---- the call sites -----------------------------------------------------------

def test_decide_and_decide_bundle_call_the_helper_after_the_transaction():
    for fn in (core_decide.decide, core_decide.decide_bundle):
        src = inspect.getsource(fn)
        assert "idp_outbox.write_request_decided(" in src
        # The call stands after the `with db.transaction()` block, not inside it.
        call = src.index("idp_outbox.write_request_decided(")
        line = src[src.rindex("\n", 0, call) + 1:call]
        assert not line.startswith(" " * 12), line


def test_withdraw_calls_the_helper_after_the_transaction():
    src = inspect.getsource(cancellation.withdraw)
    assert "idp_outbox.write_request_withdrawn(" in src
    call = src.index("idp_outbox.write_request_withdrawn(")
    assert call > src.index("audit.log_in(")
    line = src[src.rindex("\n", 0, call) + 1:call]
    assert not line.startswith(" " * 8), line


def test_the_expiry_job_calls_the_helper():
    path = Path(__file__).resolve().parent.parent / "scripts" / "expire_stale_pending.py"
    src = path.read_text()
    assert "idp_outbox.write_request_expired(" in src
    assert src.index("idp_outbox.write_request_expired(") > src.index("request_expired")
