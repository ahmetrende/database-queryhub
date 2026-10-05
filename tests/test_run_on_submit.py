"""A super-admin can choose where one query runs: submit and storage.

`POST /queries` takes `runOn` (auto | primary | replica) and `replicaId`. What
this file pins is the part decided before anything runs:

- only a super-admin may choose, and anyone else is REFUSED rather than quietly
  run as auto -- the rule `unmasked` already follows;
- a replica must be able to run the statement (read-only) and must be one of
  THIS connection's enabled replicas; the id may be left out only when there is
  exactly one, and the row then names that one;
- the choice is stored on the request (`requests.run_on`) and reaches every
  path that hands a row to the executor, the scheduler included;
- the batch endpoint and the Slack paths never set it.

Execution -- where the choice is honoured or ignored -- is
test_run_on_execution.py.
"""
from __future__ import annotations

import contextlib
import dataclasses
import inspect
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from queryhub import core_submit as cs
from queryhub import executor, lifecycle, replicas, targets
from queryhub.web import app as web_app
from queryhub.web import deps, routes_queries

ROOT = Path(__file__).resolve().parents[1]
SUPER = "U0EXAMPLE001"
PLAIN = "U0EXAMPLE002"
READ = "SELECT id, status FROM orders WHERE id = 5"
WRITE = "UPDATE orders SET status = 'x' WHERE id = 5"
ONE = [{"id": 99, "alias": "prod-ledger-read-1", "host": "r1.example.test", "port": 5432}]
TWO = ONE + [{"id": 100, "alias": "prod-ledger-read-2", "host": "r2.example.test",
              "port": 5432}]


def _target(engine="postgres"):
    return targets.TargetServer(
        id=7, alias="prod-ledger", host="primary.example.test", port=5432,
        default_database="ledger", username="reader", enabled=True, notes=None,
        engine=engine)


@pytest.fixture
def submit(monkeypatch):
    """validate_submission all the way to a Prepared, with every collaborator
    that would reach a database stubbed. `go(...)` returns the Prepared or the
    Rejection."""
    state = {"super": True, "replicas": ONE, "engine": "postgres"}
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: state["super"])
    monkeypatch.setattr(cs.targets, "get", lambda tid: _target(state["engine"]))
    monkeypatch.setattr(cs.teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ddl"})
    monkeypatch.setattr(cs.teams, "effective_mode_for_database", lambda *a: "ddl")
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: False)
    monkeypatch.setattr(cs.auto_approve, "effective_grant", lambda *a, **k: None)
    monkeypatch.setattr(cs.db, "fetch_one", lambda sql, params=None: None)  # no duplicate
    monkeypatch.setattr(cs.replicas, "replicas_of",
                        lambda pid: state["replicas"] if pid == 7 else [])

    def go(*, run_on="auto", replica_id=None, sql=READ, super_admin=True,
           replica_rows=ONE, engine="postgres"):
        state.update(super=super_admin, replicas=replica_rows, engine=engine)
        return cs.validate_submission(
            SUPER if super_admin else PLAIN, "Ex", target_server_id=7,
            database_name=None, query=sql, justification="because",
            run_on=run_on, replica_id=replica_id)

    return go


# --- who may choose ---------------------------------------------------------------

@pytest.mark.parametrize("run_on", ["primary", "replica"])
def test_anyone_but_a_super_admin_is_refused_not_downgraded(submit, run_on):
    out = submit(run_on=run_on, super_admin=False)
    assert isinstance(out, cs.Rejection)
    assert out.reason == "run_on_not_super_admin"
    assert out.message == "Only a super-admin can choose where a query runs."


def test_auto_is_what_everyone_else_gets_and_it_stores_nothing(submit):
    out = submit(super_admin=False)
    assert isinstance(out, cs.Prepared) and out.run_on is None


def test_an_unknown_choice_is_refused(submit):
    out = submit(run_on="elsewhere")
    assert isinstance(out, cs.Rejection) and out.reason == "run_on_invalid"


# --- what a replica has to be -----------------------------------------------------

def test_a_replica_runs_read_only_statements_only(submit):
    out = submit(run_on="replica", replica_id="99", sql=WRITE)
    assert isinstance(out, cs.Rejection)
    assert out.reason == "replica_read_only"
    assert out.message == "A replica runs read-only statements only."


@pytest.mark.parametrize("case", ["no replica", "not postgres", "someone else's",
                                  "not a number"])
def test_a_replica_must_be_one_of_this_connections_own(submit, case):
    kw = {"no replica": dict(replica_rows=[]),
          "not postgres": dict(engine="clickhouse", replica_id="99"),
          "someone else's": dict(replica_id="12"),
          "not a number": dict(replica_id="prod-ledger-read-1")}[case]
    out = submit(run_on="replica", **kw)
    assert isinstance(out, cs.Rejection), case
    assert out.reason == "replica_unknown", case
    assert out.message == "That replica does not belong to this connection."


def test_the_only_replica_needs_no_id_and_the_row_names_it(submit):
    out = submit(run_on="replica")
    assert isinstance(out, cs.Prepared)
    assert out.run_on == "replica:99"


def test_with_several_replicas_the_id_picks_one(submit):
    out = submit(run_on="replica", replica_id="100", replica_rows=TWO)
    assert isinstance(out, cs.Prepared) and out.run_on == "replica:100"


def test_with_several_replicas_leaving_the_id_out_is_refused(submit):
    out = submit(run_on="replica", replica_rows=TWO)
    assert isinstance(out, cs.Rejection) and out.reason == "replica_required"
    assert out.message == "Choose one replica. This connection has 2 replicas."


def test_primary_is_stored_as_primary_for_any_tier(submit):
    assert submit(run_on="primary").run_on == "primary"
    assert submit(run_on="primary", sql=WRITE).run_on == "primary"


def test_the_prepared_submission_carries_the_choice():
    assert "run_on" in {f.name for f in dataclasses.fields(cs.Prepared)}


# --- the HTTP contract ------------------------------------------------------------

@pytest.fixture
def web(monkeypatch):
    """The real app, identity through dependency_overrides, the real
    validate_submission behind it (stubbed below the database line)."""
    logging.disable(logging.CRITICAL)
    state = {"uid": SUPER, "super": True, "replicas": ONE, "engine": "postgres"}
    app = web_app.create_app()
    app.dependency_overrides[deps.current_user] = lambda: {
        "sub": state["uid"], "provider": "slack", "sid": "s1", "name": "Ex"}
    monkeypatch.setattr(deps.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(routes_queries, "_target_by_alias",
                        lambda a: _target(state["engine"]) if a == "prod-ledger" else None)
    monkeypatch.setattr(cs.teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ddl"})
    monkeypatch.setattr(cs.teams, "effective_mode_for_database", lambda *a: "ddl")
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: state["super"])
    monkeypatch.setattr(cs.targets, "get", lambda tid: _target(state["engine"]))
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: False)
    monkeypatch.setattr(cs.auto_approve, "effective_grant", lambda *a, **k: None)
    monkeypatch.setattr(cs.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(cs.replicas, "replicas_of", lambda pid: state["replicas"])
    refused = []
    monkeypatch.setattr(routes_queries.db, "execute",
                        lambda sql, params=None: refused.append(params))
    # Where an accepted submission would go next; the tests stop here.
    reached = {}

    def stop(prep, draft_id=None):
        reached["prep"] = prep
        raise RuntimeError("stop: the refusal checks are behind us")
    monkeypatch.setattr(routes_queries.core_submit, "create_request", stop)
    c = TestClient(app, raise_server_exceptions=False)
    c.state, c.reached, c.refused = state, reached, refused
    yield c
    logging.disable(logging.NOTSET)


def _post(c, **body):
    return c.post("/api/queries", json={"connectionId": "prod-ledger",
                                        "databaseId": None, "sql": READ, **body})


def _error(r):
    body = r.json()
    return body.get("error") or body.get("detail") or body


def test_http_non_super_admin_gets_403_forbidden(web):
    web.state.update(uid=PLAIN, super=False)
    r = _post(web, runOn="primary")
    assert r.status_code == 403, r.text
    err = _error(r)
    assert err["code"] == "forbidden"
    assert err["message"] == "Only a super-admin can choose where a query runs."
    assert web.refused, "a web refusal is recorded in submission_failures"


def test_http_a_write_on_a_replica_is_400_bad_request(web):
    r = _post(web, runOn="replica", replicaId="99", sql=WRITE)
    assert r.status_code == 400, r.text
    assert _error(r) == {"code": "bad_request",
                         "message": "A replica runs read-only statements only."}


@pytest.mark.parametrize("state,body", [
    ({"replicas": []}, {"replicaId": "99"}),
    ({"engine": "mssql"}, {"replicaId": "99"}),
    ({}, {"replicaId": "12"}),
])
def test_http_a_replica_that_is_not_this_connections_is_400(web, state, body):
    web.state.update(state)
    r = _post(web, runOn="replica", **body)
    assert r.status_code == 400, r.text
    assert _error(r) == {"code": "bad_request",
                         "message": "That replica does not belong to this connection."}


def test_http_several_replicas_and_no_id_is_400(web):
    web.state.update(replicas=TWO)
    r = _post(web, runOn="replica")
    assert r.status_code == 400
    assert _error(r) == {"code": "bad_request",
                         "message": "Choose one replica. This connection has 2 replicas."}


def test_http_a_scheduled_submit_carries_the_choice(web, monkeypatch):
    """runOn travels with a scheduled submit, as unmasked does: stored now,
    applied (and re-derived) when the scheduler runs it."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(routes_queries.profile_sync, "lookup_tz", lambda uid: "UTC")
    monkeypatch.setattr(cs.profile_sync, "lookup_tz", lambda uid: "UTC")
    run_at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    _post(web, runOn="replica", schedule={"runAt": run_at.isoformat()})
    prep = web.reached["prep"]
    assert prep.sched_for is not None and prep.run_on == "replica:99"


def test_http_an_unknown_run_on_is_rejected_by_the_schema(web):
    assert _post(web, runOn="somewhere").status_code == 422


def test_http_an_accepted_choice_reaches_create_request_resolved(web):
    _post(web, runOn="replica")
    assert web.reached["prep"].run_on == "replica:99"


def test_http_replica_id_is_read_only_with_run_on_replica(web):
    """A client that keeps the last replica in state while switching back to
    Auto or Primary is not refused for it; the id is simply not used."""
    _post(web, runOn="primary", replicaId="12")
    assert web.reached["prep"].run_on == "primary"
    _post(web, replicaId="12")
    assert web.reached["prep"].run_on is None


def test_http_an_older_client_that_sends_neither_field_runs_auto(web):
    _post(web)
    assert web.reached["prep"].run_on is None


def test_the_reasons_map_to_the_contract_statuses():
    assert routes_queries._REJECTION_HTTP["run_on_not_super_admin"] == (403, "forbidden")
    for reason in ("replica_read_only", "replica_unknown", "replica_required"):
        assert routes_queries._REJECTION_HTTP[reason] == (400, "bad_request"), reason


def test_the_batch_endpoint_never_carries_a_choice():
    """A bundle is one approval for many statements; one switch standing for
    all of them is the state `unmasked` was kept out of batch for."""
    assert "runOn" not in routes_queries.BatchIn.model_fields
    assert "runOn" not in routes_queries.BatchItemIn.model_fields
    assert "run_on" not in inspect.getsource(routes_queries.submit_batch)


# --- storage ----------------------------------------------------------------------

class _Cur:
    """Answers the create transaction's statements by what they are: no open
    requests, no duplicate, and the new row's id for the INSERT."""
    def __init__(self, box):
        self.box = box
        self.rowcount = 1
        self.last = ""

    def execute(self, sql, params=None):
        self.last = " ".join(sql.split())
        self.box["sql"].append((self.last, params))

    def fetchone(self):
        if self.last.startswith("SELECT count(*)"):
            return {"n": 0}
        if self.last.startswith("INSERT INTO requests"):
            return {"id": 42}
        return None


def _prep(run_on):
    return cs.Prepared(
        user_id=SUPER, user_name="Ex", target=_target(), database="ledger",
        query=READ, required_mode="ro", justification=None, wants_result=True,
        result_format="csv", sched_for=None, explain_plan=None, risk_summary=None,
        origin="web", run_on=run_on)


@pytest.mark.parametrize("super_admin", [True, False])
def test_create_request_writes_the_choice_on_both_insert_paths(monkeypatch, super_admin):
    """Auto-approved (a super-admin's own submission) and pending (anyone else,
    whose row always stores NULL) are two separate INSERTs; both carry it."""
    box = {"sql": [], "audit": []}

    @contextlib.contextmanager
    def txn():
        yield _Cur(box)
    monkeypatch.setattr(cs.db, "transaction", txn)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: super_admin)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(cs.auto_approve, "effective_grant", lambda *a, **k: None)
    monkeypatch.setattr(cs.auto_approve, "fingerprint_cache_hit", lambda *a, **k: None)
    monkeypatch.setattr(cs.ast_safety, "fingerprint", lambda *a, **k: "fp")
    monkeypatch.setattr(cs.audit, "log_in",
                        lambda cur, rid, a, n, action, details=None:
                        box["audit"].append((action, details)))
    stored = "replica:99" if super_admin else None
    out = cs.create_request(_prep(stored))
    assert isinstance(out, cs.Outcome)
    sql, params = next((s, p) for s, p in box["sql"] if s.startswith("INSERT INTO requests"))
    columns = sql[sql.index("(") + 1:sql.index(")")].replace(" ", "").split(",")
    assert "run_on" in columns
    assert sql.count("%s") == len(params), "placeholders and values disagree"
    # The value sits at the column's own position: NOW() fills decided_at on
    # the auto-approved path without a placeholder, so count only up to it.
    values = sql[sql.index("VALUES (") + 8:sql.index(") RETURNING")].replace(" ", "").split(",")
    placeholders = [i for i, v in enumerate(values) if v.startswith("%s")]
    assert params[placeholders.index(columns.index("run_on"))] == stored
    assert sql.endswith(f"RETURNING {cs.REQUEST_RETURNING}")
    submitted = dict(box["audit"])["submitted"]
    assert submitted.get("run_on") == stored


def test_the_row_returned_to_the_executor_carries_the_choice():
    assert "run_on" in {c.strip() for c in cs.REQUEST_RETURNING.split(",")}


def test_the_scheduler_hands_the_executor_the_same_columns(monkeypatch):
    """A scheduled request reaches `submit` through the scheduler's own
    RETURNING. It used to name its own columns and had fallen behind: no
    `unmasked`, so a scheduled unmasked request ran masked -- and `run_on`
    would have been dropped the same way."""
    box = {"sql": [], "submitted": []}
    row = {"id": 5, "run_on": "replica:99", "unmasked": True,
           "requester_dm_channel_id": None, "requester_dm_message_ts": None}

    class Cur:
        def execute(self, sql, params=None):
            box["sql"].append(" ".join(sql.split()))

        def fetchall(self):
            return [dict(row)]

    @contextlib.contextmanager
    def txn():
        yield Cur()
    monkeypatch.setattr(executor.db, "transaction", txn)
    monkeypatch.setattr(executor.audit, "log_in", lambda *a, **k: None)
    monkeypatch.setattr(executor.notifications, "update_user_scheduled_dm",
                        lambda *a, **k: None)
    monkeypatch.setattr(executor, "submit",
                        lambda r, client: box["submitted"].append(r))
    assert executor.dispatch_due(None) == 1
    returning = box["sql"][0].split("RETURNING", 1)[1]
    cols = {c.strip() for c in returning.split(",")}
    assert {"run_on", "unmasked"} <= cols
    assert box["submitted"][0]["run_on"] == "replica:99"
    assert executor._SCHEDULED_SELECT_COLS is cs.REQUEST_RETURNING


def test_the_migration_adds_the_column_and_its_shape_check():
    text = (ROOT / "migrations" / "137_request_run_on.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS run_on TEXT" in text
    assert "requests_run_on_shape" in text and "IF NOT EXISTS" in text
    assert "'^replica:[1-9][0-9]*$'" in text


@pytest.mark.parametrize("stored,expected", [
    (None, replicas.AUTO), ("", replicas.AUTO), ("primary", replicas.PRIMARY),
    ("replica:99", replicas.RunOn("replica", 99)),
    # Shapes the CHECK refuses read as a replica that does not exist, which
    # the executor refuses: an unreadable choice never becomes auto.
    ("replica:x", replicas.RunOn("replica", None)),
    ("replica:0", replicas.RunOn("replica", None)),
    ("somewhere", replicas.RunOn("replica", None)),
])
def test_the_stored_form_reads_back(stored, expected):
    assert replicas.parse_run_on(stored) == expected


def test_the_stored_form_round_trips():
    for choice in (replicas.AUTO, replicas.PRIMARY, replicas.RunOn("replica", 99)):
        assert replicas.parse_run_on(replicas.run_on_value(choice)) == choice


def test_resubmit_does_not_copy_the_choice():
    """Edit & resubmit reopens the modal for the request's own requester; the
    modal has no such choice, so a resubmitted request runs auto. Pinned
    because copying intents forward is how one would leak onto a request
    somebody else then makes."""
    from queryhub.slack_app import handlers
    src = inspect.getsource(handlers.handle_resubmit)
    assert "run_on" not in src and "unmasked" not in src
