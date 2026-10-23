"""PLA-1592 U6, U8, U9 and U12 on a real database.

The mocked tests pin what the code asks for. Only PostgreSQL shows that the
SQL does it:

  * U12: a batch decision locks its items, so a decision from somewhere else
    waits and then finds nothing to decide. A failure half way rolls the
    first writes back. A conflict changes nothing.
  * U6: the grouped read gives each person their newest request, and a draft
    does not count.
  * U8: the window reaches the facts view, and only a person's decisions
    count as decision time.
  * U9: a plan stored as jsonb comes back as the list that the mapper reads.

Every row uses ids, names and an alias that no real person or target has, and
the tests delete only those rows. The audit log is append-only, so its rows
stay behind. Each test reads only the audit rows that come after its start.
"""
import json
import os
import threading
import time

import pytest

from queryhub import core_decide, db
from queryhub.web import metrics, routes_admin

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

ALIAS = "it-queue-metrics"
REQ, DBA, IDLE = "U0QMETRIC1", "U0QMETRIC2", "U0QMETRIC3"
PEOPLE = [REQ, DBA, IDLE]


def _wipe():
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        cur.execute("DELETE FROM requests WHERE requester_slack_id = ANY(%s)", (PEOPLE,))
        cur.execute("DELETE FROM request_bundles WHERE requester_slack_id = ANY(%s)",
                    (PEOPLE,))
        cur.execute("DELETE FROM requesters WHERE slack_user_id = ANY(%s)", (PEOPLE,))
        cur.execute("SELECT principal_id FROM principal_identity "
                    " WHERE provider = 'slack' AND external_id = ANY(%s)", (PEOPLE,))
        pids = [r["principal_id"] for r in cur.fetchall()]
        cur.execute("DELETE FROM principal_identity WHERE principal_id = ANY(%s)", (pids,))
        cur.execute("DELETE FROM principal WHERE id = ANY(%s)", (pids,))
        cur.execute("DELETE FROM auth_event_outbox WHERE slack_user_id = ANY(%s)", (PEOPLE,))
        cur.execute("DELETE FROM target_servers WHERE alias = %s", (ALIAS,))


@pytest.fixture
def target():
    _wipe()
    yield db.insert_returning(
        "INSERT INTO target_servers (alias, host, default_database, username, "
        "  password_encrypted, enabled) "
        "VALUES (%s, '127.0.0.1', 'app', 'unused', 'unused', FALSE) RETURNING id",
        (ALIAS,))["id"]
    _wipe()


@pytest.fixture
def mark():
    return db.fetch_one("SELECT COALESCE(max(id), 0) AS n FROM audit_log")["n"]


def _request(target_id, *, status="pending", bundle=None, position=None,
             query="UPDATE ledger SET a = 1 WHERE id = 1", requester=REQ,
             created="now()", decided=None, decided_by=None, plan=None):
    """One requests row. `created` and `decided` are SQL expressions."""
    return db.insert_returning(
        f"INSERT INTO requests (requester_slack_id, requester_name, target_server_id, "
        f"  database_name, query, status, bundle_id, position, required_tier, engine, "
        f"  decided_by_slack_id, decided_by_name, explain_plan, created_at, decided_at) "
        f"VALUES (%s, 'Jordan Ray', %s, 'app', %s, %s, %s, %s, 'rw', 'postgres', "
        f"  %s, %s, %s::jsonb, {created}, {decided or 'NULL'}) RETURNING id",
        (requester, target_id, query, status, bundle, position, decided_by,
         decided_by and "Sam Archer", None if plan is None else json.dumps(plan)))["id"]


def _bundle(target_id, statuses):
    """A batch whose items have these statuses. A waiver (`AUTO`) approved
    each item that is not pending, at submit."""
    bid = db.insert_returning(
        "INSERT INTO request_bundles (requester_slack_id, requester_name) "
        "VALUES (%s, 'Jordan Ray') RETURNING id", (REQ,))["id"]
    ids = [_request(target_id, status=s, bundle=bid, position=i,
                    decided="now()" if s != "pending" else None,
                    decided_by="AUTO" if s != "pending" else None)
           for i, s in enumerate(statuses, start=1)]
    return bid, ids


def _status(*ids):
    rows = db.fetch_all("SELECT id, status::text AS s FROM requests WHERE id = ANY(%s)",
                        (list(ids),))
    by_id = {r["id"]: r["s"] for r in rows}
    return [by_id[i] for i in ids]


def _audit(mark, ids):
    return db.fetch_all("SELECT request_id, action, details FROM audit_log "
                        " WHERE id > %s AND request_id = ANY(%s) ORDER BY id",
                        (mark, list(ids)))


def _decide(bid, decision="reject", note="the batch needs a WHERE clause", may=None):
    return core_decide.decide_bundle(bid, decision, by_id=DBA, by_name="Sam Archer",
                                     reason=note, may_decide=may or (lambda row: True))


# ---- U12 --------------------------------------------------------------------------

def test_a_batch_is_decided_whole_and_each_item_has_its_audit_row(target, mark):
    bid, (auto, a, b) = _bundle(target, ["completed", "pending", "pending"])
    out = _decide(bid)
    assert [o.row["id"] for o in out] == [a, b]
    assert _status(auto, a, b) == ["completed", "rejected", "rejected"]
    rows = _audit(mark, [auto, a, b])
    assert [(r["request_id"], r["action"]) for r in rows] == [(a, "rejected"), (b, "rejected")]
    assert all(r["details"] == {"reason": "the batch needs a WHERE clause",
                                "bulk": True, "bundle_id": bid} for r in rows)
    decided = db.fetch_all("SELECT decided_by_slack_id, decision_reason FROM requests "
                           " WHERE id = ANY(%s)", ([a, b],))
    assert {(r["decided_by_slack_id"], r["decision_reason"]) for r in decided} == {
        (DBA, "the batch needs a WHERE clause")}


def test_an_item_decided_elsewhere_leaves_the_batch_as_it_was(target, mark):
    bid, (a, b) = _bundle(target, ["pending", "pending"])
    assert core_decide.decide(a, "approve", by_id="U0QOTHER01", by_name="Other") is not None
    after_slack = db.fetch_one("SELECT COALESCE(max(id), 0) AS n FROM audit_log")["n"]
    with pytest.raises(core_decide.BundleConflict) as e:
        _decide(bid)
    assert e.value.item_ids == [a]
    assert _status(a, b) == ["approved", "pending"]
    assert _audit(after_slack, [a, b]) == []


def test_a_failure_half_way_rolls_the_first_write_back(target, mark, monkeypatch):
    bid, (a, b) = _bundle(target, ["pending", "pending"])
    real = core_decide._write
    calls = []

    def write_then_fail(cur, rid, *args, **kw):
        calls.append(rid)
        if len(calls) == 2:
            raise RuntimeError("the database went away")
        return real(cur, rid, *args, **kw)
    monkeypatch.setattr(core_decide, "_write", write_then_fail)
    with pytest.raises(RuntimeError):
        _decide(bid)
    assert calls == [a, b]
    assert _status(a, b) == ["pending", "pending"]
    assert _audit(mark, [a, b]) == []


def test_a_decision_from_elsewhere_waits_for_the_lock_and_then_finds_nothing(target):
    """The batch decision holds its items while it checks them. A Slack click
    on one item in that moment blocks, then sees the item decided."""
    bid, (a, b) = _bundle(target, ["pending", "pending"])
    locked, go = threading.Event(), threading.Event()
    result = {}

    def hold(row):
        locked.set()
        go.wait(5)
        return True

    def batch():
        result["batch"] = _decide(bid, may=hold)

    def slack():
        result["slack"] = core_decide.decide(a, "approve", by_id="U0QOTHER01",
                                             by_name="Other")

    t_batch = threading.Thread(target=batch)
    t_batch.start()
    assert locked.wait(5)
    t_slack = threading.Thread(target=slack)
    t_slack.start()
    time.sleep(0.5)
    assert t_slack.is_alive(), "the single decision did not wait for the lock"
    go.set()
    t_batch.join(10)
    t_slack.join(10)
    assert [o.row["id"] for o in result["batch"]] == [a, b]
    assert result["slack"] is None
    assert _status(a, b) == ["rejected", "rejected"]


def test_the_route_decides_the_batch_and_answers_409_the_second_time(
        target, mark, monkeypatch):
    bid, (a, b) = _bundle(target, ["pending", "pending"])
    effects = []
    monkeypatch.setattr(routes_admin.admin, "require_admin", lambda c, n="review", **k: DBA)
    monkeypatch.setattr(routes_admin.admins, "can_approve", lambda uid, row: uid == DBA)
    monkeypatch.setattr(routes_admin, "_slack_client", lambda: None)
    monkeypatch.setattr(routes_admin.core_decide, "apply_effects",
                        lambda client, o: effects.append(o.row["id"]))
    body = routes_admin.DecisionIn(decision="changes", note="split it in two")
    out = routes_admin.admin_bundle_decision(bid, body, claims={"sub": DBA})
    assert out["items"] == [{"id": str(a), "status": "rejected"},
                            {"id": str(b), "status": "rejected"}]
    assert _status(a, b) == ["changes_requested", "changes_requested"]
    assert effects == [a, b]
    with pytest.raises(Exception) as e:
        routes_admin.admin_bundle_decision(bid, body, claims={"sub": DBA})
    assert e.value.status_code == 409
    assert e.value.detail["items"] == [str(a), str(b)]
    assert len(_audit(mark, [a, b])) == 2


def test_the_route_refuses_a_batch_outside_the_scope_and_writes_nothing(
        target, mark, monkeypatch):
    bid, (a, b) = _bundle(target, ["pending", "pending"])
    monkeypatch.setattr(routes_admin.admin, "require_admin", lambda c, n="review", **k: DBA)
    monkeypatch.setattr(routes_admin.admins, "can_approve", lambda uid, row: row["id"] == a)
    body = routes_admin.DecisionIn(decision="approve", note=None)
    with pytest.raises(Exception) as e:
        routes_admin.admin_bundle_decision(bid, body, claims={"sub": DBA})
    assert e.value.status_code == 403
    assert _status(a, b) == ["pending", "pending"]
    assert _audit(mark, [a, b]) == []


# ---- U6 ---------------------------------------------------------------------------

def test_the_last_query_is_the_newest_request_and_a_draft_does_not_count(target, monkeypatch):
    with db.transaction() as cur:
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        for pid in (REQ, IDLE):
            cur.execute("INSERT INTO requesters (slack_user_id, email, name, enabled) "
                        "VALUES (%s, %s, %s, TRUE)",
                        (pid, f"{pid.lower()}@example.invalid", f"IT Person {pid[-1]}"))
    _request(target, status="completed", query="SELECT 1",
             created="now() - interval '3 days'")
    newest = _request(target, status="rejected", query="SELECT 2",
                      created="now() - interval '2 days'")
    # Newer than both, and not a query.
    db.execute("INSERT INTO requests (requester_slack_id, query, database_name, status, "
               "  created_at) VALUES (%s, '', '', 'draft', now())", (REQ,))
    monkeypatch.setattr(routes_admin.admin, "require_admin", lambda c, s: "U0QADMIN01")
    # The caller is a super-admin here: a DBA reads id and name only.
    monkeypatch.setattr(routes_admin.admins, "is_super_admin", lambda uid: True)
    people = {p["id"]: p for p in routes_admin.admin_people(claims={"sub": "U0QADMIN01"})["people"]}
    want = db.fetch_one("SELECT created_at FROM requests WHERE id = %s", (newest,))["created_at"]
    assert people[REQ]["lastQueryAt"] == want.isoformat()
    assert people[IDLE]["lastQueryAt"] is None


# ---- U8 ---------------------------------------------------------------------------

@pytest.fixture
def reporting_reaches_back():
    """The facts view starts at `report_start_date`. A fresh database seeds it
    with the day the migration ran, which would hide every row the test dates
    in the past. Moved back for the test only, and restored after it."""
    row = db.fetch_one("SELECT value FROM bot_config WHERE key = 'report_start_date'")
    db.execute("INSERT INTO bot_config (key, value) VALUES ('report_start_date', "
               "  (CURRENT_DATE - 60)::text) "
               "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    yield
    if row is None:
        db.execute("DELETE FROM bot_config WHERE key = 'report_start_date'")
    else:
        db.execute("UPDATE bot_config SET value = %s WHERE key = 'report_start_date'",
                   (row["value"],))


def test_the_window_reaches_the_facts_and_only_a_persons_decisions_count(
        target, reporting_reaches_back):
    _request(target, status="completed", created="now() - interval '3 days'",
             decided="now() - interval '3 days' + interval '10 minutes'", decided_by=DBA)
    _request(target, status="rejected", created="now() - interval '20 days'",
             decided="now() - interval '20 days' + interval '2 hours'", decided_by=DBA)
    _request(target, status="completed", created="now() - interval '1 day'",
             decided="now() - interval '1 day'", decided_by="AUTO")
    # Approved, then withdrawn while it waited for its time: the withdrawal
    # wrote `decided_at` again, so its seconds are no decision time.
    _request(target, status="cancelled", created="now() - interval '1 day'",
             decided="now() - interval '1 day' + interval '30 minutes'", decided_by=DBA)
    _request(target, status="completed", created="now() - interval '1 day'",
             decided="now() - interval '1 day' + interval '1 minute'", decided_by=REQ)

    def mine(window):
        out = metrics.build_metrics(window)
        server = [s for s in out["decisionTime"]["byServer"] if s["name"] == ALIAS]
        used = [t["count"] for t in out["targetUsage"] if t["name"] == ALIAS]
        return server, used

    week, week_used = mine("7d")
    month, month_used = mine("30d")
    assert week == [{"name": ALIAS, "requests": 1, "p50Sec": 600.0, "p90Sec": 600.0}]
    assert month == [{"name": ALIAS, "requests": 2, "p50Sec": 3900.0, "p90Sec": 6540.0}]
    assert week_used == [4] and month_used == [5]


# ---- U9 ---------------------------------------------------------------------------

def test_a_stored_plan_reaches_the_queue_item(target, monkeypatch):
    with db.transaction() as cur:
        cur.execute("EXPLAIN (FORMAT JSON, ANALYZE OFF, COSTS ON, BUFFERS OFF) "
                    "SELECT r.id FROM requests r JOIN target_servers t "
                    "  ON t.id = r.target_server_id WHERE r.status = 'pending'")
        plan = cur.fetchone()["QUERY PLAN"]
    rid = _request(target, query="SELECT r.id FROM requests r", plan=plan)
    bare = _request(target, query="SELECT 3")
    monkeypatch.setattr(routes_admin.admin, "require_admin", lambda c, s: "U0QADMIN01")
    monkeypatch.setattr(routes_admin.admins, "can_approve", lambda uid, row: True)
    items = {int(i["id"]): i for i in routes_admin.admin_queue(claims={"sub": "U0QADMIN01"})["queue"]}
    rows, tables = items[rid]["estRows"], items[rid]["estTables"]
    assert rows == plan[0]["Plan"]["Plan Rows"]
    assert set(tables) == {"requests", "target_servers"}
    assert items[bare]["estRows"] is None and items[bare]["estTables"] == []
