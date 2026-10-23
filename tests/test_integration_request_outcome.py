"""How a request closed, and when a pending one expires, on a real database
(PLA-1592 U11).

tests/test_request_outcome.py pins the rule with fakes. Here the writers run
for real, and the readers read what they wrote:

- The expiry job (scripts/expire_stale_pending.py) closes a stale request.
  Then `GET /queries/{id}` and `GET /history` say `expired`.
- A web withdrawal (`cancellation.withdraw`) and a cancel of a scheduled query
  say `withdrawn`.
- A pending request carries `expiresAt`. The job closes it only after that
  time.

The expiry job closes EVERY stale pending request in the database. So the test
that runs it first asks the job's own query what it would close, and skips
itself when that is more than the fixture. A stray run against a live database
never closes a real request.

Every row uses ids and an alias that no real person or target has, and the
tests delete only those rows. The audit log is append-only, so its rows stay
behind.
"""
import importlib.util
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from queryhub import audit, cancellation, db, pending_expiry
from queryhub.web import app as web_app
from queryhub.web import deps, mapping, routes_data, routes_queries

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

ME = "U0ITOUTCM01"
ALIAS = "it-outcome-fixture"
ROOT = Path(__file__).resolve().parents[1]


def _wipe():
    db.execute("DELETE FROM requests WHERE requester_slack_id = %s", (ME,))
    db.execute("DELETE FROM target_servers WHERE alias = %s", (ALIAS,))


@pytest.fixture
def target():
    _wipe()
    yield db.insert_returning(
        "INSERT INTO target_servers (alias, host, default_database, username, "
        "  password_encrypted, enabled) "
        "VALUES (%s, 'fixture.example.test', 'app', 'unused', 'unused', FALSE) "
        "RETURNING id", (ALIAS,))["id"]
    _wipe()


def _request(target_id, status="pending", age=timedelta(0), **extra):
    cols = {"requester_slack_id": ME, "target_server_id": target_id,
            "database_name": "app", "query": "SELECT 1", "status": status, **extra}
    names = ", ".join(cols)
    holders = ", ".join(["%s"] * len(cols))
    rid = db.insert_returning(
        f"INSERT INTO requests ({names}) VALUES ({holders}) RETURNING id",
        tuple(cols.values()))["id"]
    if age:
        db.execute("UPDATE requests SET created_at = NOW() - %s WHERE id = %s", (age, rid))
    return rid


@pytest.fixture
def client(monkeypatch):
    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(routes_queries.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(routes_data.deps, "require_whitelisted", lambda claims: None)
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": ME, "provider": "slack", "sid": "s1", "name": "Jordan Ray"}
    yield TestClient(app, raise_server_exceptions=False)
    logging.disable(logging.NOTSET)


def _load_job():
    spec = importlib.util.spec_from_file_location(
        "expire_stale_pending_it", ROOT / "scripts" / "expire_stale_pending.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_expiry_job_writes_what_the_outcome_reads(target, client, monkeypatch):
    stale = _request(target, age=timedelta(hours=2))
    fresh = _request(target)
    job = _load_job()
    monkeypatch.setattr(pending_expiry, "enabled", lambda: True)
    monkeypatch.setattr(pending_expiry, "hours", lambda: 1)
    would_close = {r["id"] for r in job.find_stale(1)}
    if would_close != {stale}:
        pytest.skip("other stale pending requests exist: not a scratch database")

    before = client.get(f"/api/queries/{stale}").json()
    created = db.fetch_one("SELECT created_at FROM requests WHERE id = %s", (stale,))
    assert before["expiresAt"] == (created["created_at"] + timedelta(hours=1)).isoformat()
    assert before["outcome"] is None

    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_requester", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["expire_stale_pending.py", "--commit"])
    assert job.main() == 0

    after = client.get(f"/api/queries/{stale}").json()
    assert (after["status"], after["outcome"], after["expiresAt"]) == \
        ("rejected", "expired", None)
    still = client.get(f"/api/queries/{fresh}").json()
    assert (still["status"], still["outcome"]) == ("pending", None)
    history = {h["id"]: h for h in client.get("/api/history").json()["history"]}
    assert history[str(stale)]["outcome"] == "expired"
    assert history[str(fresh)]["outcome"] is None


def test_a_withdrawal_and_a_cancelled_schedule_read_withdrawn(target, client):
    withdrawn = _request(target)
    assert cancellation.withdraw(withdrawn, ME, "Jordan Ray")
    scheduled = _request(target, status="scheduled",
                         scheduled_for=datetime.now(timezone.utc) + timedelta(days=1))
    assert client.delete(f"/api/scheduled/{scheduled}").status_code == 204
    unmarked = _request(target, status="cancelled")        # no audit row at all
    refused = _request(target, status="rejected")

    history = {h["id"]: h for h in client.get("/api/history").json()["history"]}
    assert {k: (history[str(k)]["status"], history[str(k)]["outcome"])
            for k in (withdrawn, scheduled, unmarked, refused)} == {
        withdrawn: ("rejected", "withdrawn"),
        scheduled: ("rejected", "withdrawn"),
        unmarked: ("rejected", None),
        refused: ("rejected", "denied"),
    }
    assert client.get(f"/api/queries/{withdrawn}").json()["outcome"] == "withdrawn"


def test_the_page_read_asks_only_for_the_markers(target):
    rid = _request(target)
    with db.transaction() as cur:
        audit.log_in(cur, rid, ME, "Jordan Ray", "submitted", {})
    cancellation.withdraw(rid, ME, "Jordan Ray")
    got = routes_data._outcome_audits([rid])
    assert got == {rid: {"withdrawn"}}           # `submitted` is not read
    assert set(mapping.OUTCOME_ACTIONS) >= got[rid]
