"""Auto-approve stays off on an Athena archive unless its target turns it on.

Operator decision C3 (2026-09-20): a query on an Athena archive is reviewed by a
person. The path may exist, but it stays off unless a setting turns it on. When
an archive target opened with a team read grant, nothing enforced that: a pod
captain on the team held a fleet-wide read waiver with no expiry, and
`effective_grant` returns such a waiver for an archive exactly as for any other
target, so their archive queries would have run with no review. The fingerprint
cache did the same for every member's repeat query shapes, and on Athena the
literal values a fingerprint ignores are the partitions a query scans, which is
what it costs.

`engines.auto_approve_allowed(target)` is the one answer. Every path that
decides auto-approval, or tells somebody a query will be auto-approved, asks it.
The last test here finds every caller of the two lookups in the package and
fails when one of them does not ask.

A super-admin's own submission is a separate, operator-confirmed rule and is
not affected.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from queryhub import (athena_exec, auto_approve, bundles, core_submit as cs, engines,
                           executor, lifecycle, targets, teams)

CAPTAIN = "U0EXAMPLE001"
READ = "SELECT count(*) FROM ledgers WHERE day = DATE '2026-09-01'"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
_IDS = {"postgres": 7, "mssql": 8, "clickhouse": 9, "athena": 40}


def _target(engine="postgres", engine_config=None):
    return targets.TargetServer(
        id=_IDS[engine], alias=f"example-{engine}", host=f"{engine}.example.test",
        port=443, default_database="ledger", username="reader", enabled=True,
        notes=None, engine=engine, engine_config=dict(engine_config or {}))


ARCHIVE = _target("athena")
ARCHIVE_ON = _target("athena", {"auto_approve": True})
LEDGER = _target("postgres")

# A fleet-wide read waiver with no expiry: target and database NULL.
WAIVER = {"id": 501, "slack_user_id": CAPTAIN, "max_tier": "ro",
          "target_server_id": None, "database_name": None,
          "starts_at": NOW, "expires_at": None, "reason": "pod captain",
          "granted_by": "U0EXAMPLE009"}
PRIOR = {"id": 77, "completed_at": NOW}


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("engine,expected", [
    ("postgres", True), ("mssql", True), ("clickhouse", True), ("athena", False),
])
def test_each_engine_has_a_default_and_only_athena_is_off(engine, expected):
    assert engines.spec(engine).auto_approve_default is expected
    assert engines.auto_approve_allowed(_target(engine)) is expected


def test_a_target_overrides_its_engine_in_both_directions():
    assert engines.auto_approve_allowed(ARCHIVE_ON) is True
    assert engines.auto_approve_allowed(_target("postgres", {"auto_approve": False})) is False


@pytest.mark.parametrize("value", ["true", "false", "yes", 1, 0, None, [], {}])
def test_only_a_json_boolean_is_an_answer(value):
    """A string that reads as true, or a 1, is not a decision anybody made: it
    falls back to the engine's default instead of opening the door."""
    assert engines.auto_approve_allowed(_target("athena", {"auto_approve": value})) is False
    assert engines.auto_approve_allowed(_target("postgres", {"auto_approve": value})) is True


def test_no_target_means_no_exemption():
    assert engines.auto_approve_allowed(None) is False


def test_an_engine_config_that_is_not_an_object_reads_as_the_default():
    class Odd:
        engine, engine_config = "athena", ["auto_approve", True]
    assert engines.auto_approve_allowed(Odd()) is False


# ---------------------------------------------------------------------------
# create_request: the decision
# ---------------------------------------------------------------------------

class _Cur:
    """Answers the create transaction's statements by what they are: no open
    requests, no duplicate, and id 42 for the INSERT."""

    def __init__(self, box):
        self.box, self.rowcount, self.last = box, 1, ""

    def execute(self, sql, params=None):
        self.last = " ".join(sql.split())
        self.box["sql"].append((self.last, params))

    def fetchone(self):
        if self.last.startswith("SELECT count(*)"):
            return {"n": 0}
        if self.last.startswith("INSERT INTO requests"):
            return {"id": 42}
        return None


@pytest.fixture
def create(monkeypatch):
    st = {"super": False, "waiver": None, "prior": None, "cache_asked": 0,
          "sql": [], "audit": []}

    @contextlib.contextmanager
    def txn():
        yield _Cur(st)
    monkeypatch.setattr(cs.db, "transaction", txn)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: st["super"])
    monkeypatch.setattr(cs.auto_approve, "effective_grant", lambda *a, **k: st["waiver"])

    def cache_hit(*a, **k):
        st["cache_asked"] += 1
        return st["prior"]
    monkeypatch.setattr(cs.auto_approve, "fingerprint_cache_hit", cache_hit)
    monkeypatch.setattr(cs.ast_safety, "fingerprint", lambda *a, **k: "fp-1")
    monkeypatch.setattr(cs.audit, "log_in",
                        lambda cur, rid, actor, name, action, details=None:
                        st["audit"].append((action, details)))

    def go(target):
        prep = cs.Prepared(
            user_id=CAPTAIN, user_name="Ex", target=target, database="ledger",
            query=READ, required_mode="ro", justification=None, wants_result=True,
            result_format="csv", sched_for=None, explain_plan=None,
            risk_summary=None, origin="web")
        out = cs.create_request(prep)
        assert isinstance(out, cs.Outcome), out
        insert = next((s, p) for s, p in st["sql"] if s.startswith("INSERT INTO requests"))
        return out, insert
    st["go"] = go
    return st


def _actions(st):
    return [action for action, _details in st["audit"]]


def _columns(sql):
    """The INSERT's column list. Not the whole statement: its RETURNING names
    the decision columns on both paths."""
    return sql[sql.index("(") + 1:sql.index(")")].replace(" ", "").split(",")


def test_a_fleet_wide_waiver_does_not_auto_approve_an_archive_query(create, caplog):
    create["waiver"] = WAIVER
    with caplog.at_level(logging.INFO, logger="queryhub.core_submit"):
        out, (sql, _params) = create["go"](ARCHIVE)
    assert out.auto_approved is False and out.aa_grant is None
    assert "decided_by_slack_id" not in _columns(sql), "not the pending INSERT"
    assert _actions(create) == ["submitted"]
    assert "auto_approved" not in create["audit"][0][1]
    # The holder is told why; everywhere else their reads skip review.
    assert out.aa_warn and "Auto-approve is off for this connection" in out.aa_warn
    assert any("waiver 501 not applied" in r.getMessage() for r in caplog.records)


def test_the_target_can_let_the_waiver_decide_again(create):
    create["waiver"] = WAIVER
    out, (sql, _params) = create["go"](ARCHIVE_ON)
    assert out.auto_approved is True and out.aa_grant is WAIVER
    assert "decided_by_slack_id" in _columns(sql)
    assert _actions(create) == ["submitted", "auto_approved"]
    assert out.aa_warn is None


def test_a_fingerprint_match_does_not_auto_approve_an_archive_query(create):
    create["prior"] = PRIOR
    out, (_sql, params) = create["go"](ARCHIVE)
    assert out.auto_approved is False and out.fp_hit is None
    assert create["cache_asked"] == 0
    # Still computed and stored: the row reads like any other request's.
    assert "fp-1" in params
    # Nobody held a waiver, so there is nothing to explain.
    assert out.aa_warn is None


def test_the_target_can_let_the_cache_decide_again(create):
    create["prior"] = PRIOR
    out, _insert = create["go"](ARCHIVE_ON)
    assert out.auto_approved is True and out.fp_hit == PRIOR
    assert _actions(create) == ["submitted", "auto_approved_fingerprint"]


@pytest.mark.parametrize("engine", ["postgres", "mssql", "clickhouse"])
def test_other_engines_still_honour_a_waiver(create, engine):
    create["waiver"] = WAIVER
    out, _insert = create["go"](_target(engine))
    assert out.auto_approved is True and out.aa_grant is WAIVER
    assert out.aa_warn is None
    assert _actions(create) == ["submitted", "auto_approved"]


@pytest.mark.parametrize("engine", ["postgres", "mssql", "clickhouse"])
def test_other_engines_still_honour_the_cache(create, engine):
    create["prior"] = PRIOR
    out, _insert = create["go"](_target(engine))
    assert out.auto_approved is True and out.fp_hit == PRIOR
    assert create["cache_asked"] == 1


def test_a_super_admins_own_archive_query_still_auto_approves(create):
    create["super"] = True
    create["waiver"] = WAIVER
    out, (sql, _params) = create["go"](ARCHIVE)
    assert out.auto_approved is True
    assert out.aa_grant is None and out.fp_hit is None
    details = dict(create["audit"])["auto_approved_super"]
    assert details["reason"] == "super-admin full access"


# ---------------------------------------------------------------------------
# validate_submission: the justification exemption
# ---------------------------------------------------------------------------

@pytest.fixture
def validate(monkeypatch):
    """validate_submission with `require_justification` on, so a read without
    a reason passes only when something exempts it."""
    st = {"super": False, "target": ARCHIVE}
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(cs.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(cs.admins, "is_super_admin", lambda uid: st["super"])
    monkeypatch.setattr(cs.targets, "get", lambda tid: st["target"])
    monkeypatch.setattr(cs.teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ro"})
    monkeypatch.setattr(cs.teams, "effective_mode_for_database", lambda *a: "ro")
    monkeypatch.setattr(cs.pre_flight, "is_enabled", lambda: False)
    monkeypatch.setattr(cs.auto_approve, "effective_grant", lambda *a, **k: WAIVER)
    monkeypatch.setattr(cs.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(athena_exec, "config_of", lambda t: {})
    monkeypatch.setattr(athena_exec, "risk_hint", lambda *a, **k: None)
    monkeypatch.setattr(cs, "_archive_freshness", lambda t, c: None)
    monkeypatch.setattr(cs.cfg, "get_bool",
                        lambda key, default=False: key == "require_justification" or default)

    def go():
        return cs.validate_submission(
            CAPTAIN, "Ex", target_server_id=st["target"].id, database_name=None,
            query=READ, justification=None)
    st["go"] = go
    return st


def test_a_waiver_does_not_excuse_the_reason_on_an_archive(validate):
    """It reaches an approver, who is the reader the field is for."""
    out = validate["go"]()
    assert isinstance(out, cs.Rejection) and out.field == "justification"


@pytest.mark.parametrize("target", [LEDGER, ARCHIVE_ON], ids=["postgres", "athena-on"])
def test_a_waiver_still_excuses_it_where_it_decides(validate, target):
    validate["target"] = target
    assert isinstance(validate["go"](), cs.Prepared)


def test_a_super_admin_is_excused_on_an_archive_too(validate):
    validate["super"] = True
    assert isinstance(validate["go"](), cs.Prepared)


# ---------------------------------------------------------------------------
# the two batch paths
# ---------------------------------------------------------------------------

class _BatchCur:
    """The bundle transaction: an auto-approval UPDATE returns its new state."""

    def __init__(self):
        self.last = ""

    def execute(self, sql, params=None):
        self.last = " ".join(sql.split())

    def fetchone(self):
        return {"status": "approved"} if self.last.startswith("UPDATE requests") else None


def _bundle(cur, **kw):
    return {"bundle_id": 5,
            "item_rows": [{"id": 11 + i, "position": i + 1, "status": "pending"}
                          for i in range(len(kw["items"]))]}


@pytest.fixture
def batch_common(monkeypatch):
    st = {"audit": [], "dispatched": []}

    @contextlib.contextmanager
    def txn():
        yield _BatchCur()
    monkeypatch.setattr(cs.db, "transaction", txn)
    monkeypatch.setattr(bundles, "is_enabled", lambda: True)
    monkeypatch.setattr(bundles, "max_items", lambda: 5)
    monkeypatch.setattr(bundles, "insert_bundle_with_items", _bundle)
    monkeypatch.setattr(auto_approve, "effective_grant", lambda *a, **k: WAIVER)
    monkeypatch.setattr(executor, "submit",
                        lambda row, client: st["dispatched"].append(row["id"]))
    return st


def test_a_slack_batch_reviews_its_archive_item_and_runs_the_rest(batch_common, monkeypatch):
    from queryhub.slack_app import handlers
    st = batch_common
    st.update(notified=[], dms=[])
    by_id = {LEDGER.id: LEDGER, ARCHIVE.id: ARCHIVE}
    raw = [{"target_server_id": LEDGER.id}, {"target_server_id": ARCHIVE.id}]

    def validated(*, user_id, raw_item, bundle_justification):
        t = by_id[raw_item["target_server_id"]]
        return ({"target_server_id": t.id, "target_alias": t.alias,
                 "database_name": "ledger", "query": READ, "wants_result": True,
                 "result_format": "csv", "required_mode": "ro",
                 "explain_plan": None}, {})
    monkeypatch.setattr(handlers, "_ack_logging", lambda ack, body, mode: ack)
    monkeypatch.setattr(handlers, "_kill_switch_on", lambda: False)
    monkeypatch.setattr(handlers.modal, "parse_batch_submission",
                        lambda view: {"items": raw, "schedule_date": None,
                                      "schedule_time": None, "justification": None})
    monkeypatch.setattr(handlers, "_resolve_schedule", lambda d, t, uid: (None, None, None))
    monkeypatch.setattr(handlers.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(handlers, "_validate_batch_item", validated)
    monkeypatch.setattr(handlers.targets, "get", lambda tid: by_id.get(tid))
    monkeypatch.setattr(handlers.audit, "log_in",
                        lambda cur, rid, actor, name, action, details=None:
                        st["audit"].append((rid, action, details)))
    monkeypatch.setattr(handlers.pre_flight, "is_enabled", lambda: False)
    monkeypatch.setattr(handlers, "_dm_admins_bundle_auto_approved", lambda *a: None)
    monkeypatch.setattr(handlers.admins, "list_active", lambda: [{"slack_user_id": "U0EXAMPLE009"}])
    monkeypatch.setattr(handlers.notifications, "notify_admins_bundle",
                        lambda client, bid: st["notified"].append(bid))
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda client, uid, text=None, blocks=None: st["dms"].append(text))

    handlers.handle_batch_submission(lambda *a, **k: None,
                                     {"user": {"id": CAPTAIN, "name": "Ex"}, "view": {}}, None)

    assert st["dispatched"] == [11], "only the Postgres item runs at once"
    submitted = {rid: d for rid, action, d in st["audit"] if action == "submitted"}
    assert submitted[11]["auto_approved"] is True
    assert submitted[12]["auto_approved"] is False
    assert [rid for rid, action, _d in st["audit"] if action == "auto_approved"] == [11]
    assert st["notified"] == [5], "the archive item goes to the approvers"


def test_a_web_batch_reviews_its_archive_item_and_runs_the_rest(batch_common, monkeypatch):
    from queryhub.web import routes_queries as rq
    st = batch_common
    by_alias = {LEDGER.alias: LEDGER, ARCHIVE.alias: ARCHIVE}

    def validated(uid, name, *, target_server_id, database_name, query, **kw):
        t = next(t for t in by_alias.values() if t.id == target_server_id)
        return cs.Prepared(
            user_id=uid, user_name=name, target=t, database="ledger", query=query,
            required_mode="ro", justification=None, wants_result=True,
            result_format="csv", sched_for=None, explain_plan=None,
            risk_summary=None, origin="web")
    monkeypatch.setattr(rq.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rq.deps, "block_if_password_change_required", lambda claims: None)
    monkeypatch.setattr(rq, "_client_ctx", lambda request: (None, None))
    monkeypatch.setattr(rq, "_target_by_alias", lambda alias: by_alias.get(alias))
    monkeypatch.setattr(teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ro"})
    monkeypatch.setattr(rq.core_submit, "validate_submission", validated)
    monkeypatch.setattr(rq.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(rq.audit_mod, "log_in",
                        lambda cur, rid, actor, name, action, details=None:
                        st["audit"].append((rid, action, details)))
    monkeypatch.setattr(rq, "_bot_client", lambda: None)
    monkeypatch.setattr(rq.admins, "list_active", lambda: [])

    body = rq.BatchIn(items=[rq.BatchItemIn(connectionId=LEDGER.alias, sql=READ),
                             rq.BatchItemIn(connectionId=ARCHIVE.alias, sql=READ)])
    out = rq.submit_batch(body, request=None, claims={"sub": CAPTAIN, "name": "Ex"})

    assert st["dispatched"] == [11], "only the Postgres item runs at once"
    status = {item["queryId"]: item["status"] for item in out["items"]}
    assert status["12"] == rq.mapping.status_to_web("pending")
    assert status["11"] == rq.mapping.status_to_web("approved")
    assert [rid for rid, action, _d in st["audit"] if action == "auto_approved"] == [11]


# ---------------------------------------------------------------------------
# what the web says before anything is submitted
# ---------------------------------------------------------------------------

@pytest.fixture
def classify(monkeypatch):
    from queryhub.web import routes_queries as rq
    st = {"target": ARCHIVE, "super": False}
    monkeypatch.setattr(rq.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rq, "_target_by_alias", lambda alias: st["target"])
    monkeypatch.setattr(rq.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(rq.admins, "is_super_admin", lambda uid: st["super"])
    monkeypatch.setattr(teams, "effective_grant_for_user",
                        lambda uid, tid: {"allowed_databases": None, "mode": "ro"})
    monkeypatch.setattr(teams, "effective_mode_for_database", lambda uid, tid, d: "ro")
    monkeypatch.setattr(auto_approve, "effective_grant", lambda *a, **k: WAIVER)

    def go():
        body = rq.ClassifyIn(connectionId=st["target"].alias, databaseId="ledger", sql=READ)
        return rq.classify_query(body, claims={"sub": CAPTAIN})
    st["go"] = go
    return st


def test_the_editor_is_not_told_an_archive_query_will_auto_approve(classify):
    got = classify["go"]()
    assert got["tier"] == "RO" and got["blocked"] is False
    assert got["willAutoApprove"] is False


@pytest.mark.parametrize("target", [LEDGER, ARCHIVE_ON], ids=["postgres", "athena-on"])
def test_the_editor_is_told_where_the_waiver_decides(classify, target):
    classify["target"] = target
    assert classify["go"]()["willAutoApprove"] is True


def test_a_super_admin_is_told_their_archive_query_runs(classify):
    classify["super"] = True
    assert classify["go"]()["willAutoApprove"] is True


def test_the_connection_list_does_not_promise_it_on_an_archive(monkeypatch):
    from queryhub.web import routes_data as rd
    fleet = [LEDGER, ARCHIVE,
             dataclasses.replace(ARCHIVE_ON, id=41, alias="example-archive-on")]
    monkeypatch.setattr(rd.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rd.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(rd.targets, "list_enabled", lambda: fleet)
    monkeypatch.setattr(rd.teams, "effective_grants_for_user",
                        lambda uid, ids: {i: {"allowed_databases": None, "mode": "ro"} for i in ids})
    monkeypatch.setattr(rd, "_catalog_databases_map", lambda ids: {i: ["ledger"] for i in ids})
    monkeypatch.setattr(rd.auto_approve, "active_grants", lambda uid: [WAIVER])
    monkeypatch.setattr(rd, "_catalog_table_refs_map", lambda pairs: {})
    monkeypatch.setattr(rd, "_catalog_functions_map", lambda pairs: {})

    got = {c["id"]: c for c in rd.connections(claims={"sub": CAPTAIN})["connections"]}
    flags = {alias: (c["autoApproveRO"], [d["autoApproveRO"] for d in c["databases"]])
             for alias, c in got.items()}
    assert flags == {"example-postgres": (True, [True]),
                     "example-athena": (False, [False]),
                     "example-archive-on": (True, [True])}


# ---------------------------------------------------------------------------
# the Slack modal's badge and the read-burst nudge
# ---------------------------------------------------------------------------

def _scoped(tid, dbn=None):
    return {**WAIVER, "id": 502, "target_server_id": tid, "database_name": dbn}


@pytest.fixture
def banner(monkeypatch):
    from queryhub.slack_app import handlers, modal
    st = {"rows": [], "burst": None, "fleet": [LEDGER, ARCHIVE],
          "reach": {LEDGER.id, ARCHIVE.id}, "dms": []}
    monkeypatch.setattr(auto_approve, "active_grants", lambda pid, at=None: st["rows"])
    monkeypatch.setattr(modal, "_recent_ro_burst", lambda pid: st["burst"])
    monkeypatch.setattr(modal.targets, "get",
                        lambda tid: next((t for t in st["fleet"] if t.id == tid), None))
    monkeypatch.setattr(modal.targets, "list_enabled", lambda: st["fleet"])
    monkeypatch.setattr(modal.teams, "can_use_target", lambda pid, tid: tid in st["reach"])
    monkeypatch.setattr(handlers.notifications, "dm_requester",
                        lambda client, pid, text=None, blocks=None: st["dms"].append(blocks))

    def text():
        return json.dumps(modal._auto_approve_banner(CAPTAIN), ensure_ascii=False)
    st["text"], st["handlers"] = text, handlers
    return st


def _burst(target):
    return {"count": 3, "target_server_id": target.id, "database_name": "ledger"}


def test_an_archive_burst_is_not_offered_a_window(banner):
    """The window would be granted and never apply."""
    banner["burst"] = _burst(ARCHIVE)
    text = banner["text"]()
    assert "Running a lot of reads?" not in text
    assert '"text": "Request"' in text, "the modest button stays for elsewhere"


def test_a_burst_elsewhere_still_is(banner):
    banner["burst"] = _burst(LEDGER)
    assert "Running a lot of reads?" in banner["text"]()


def test_an_archive_burst_gets_no_dm_and_one_elsewhere_does(banner):
    banner["burst"] = _burst(ARCHIVE)
    banner["handlers"]._maybe_dm_ro_burst(None, CAPTAIN, "ro")
    assert banner["dms"] == []
    banner["burst"] = _burst(LEDGER)
    banner["handlers"]._maybe_dm_ro_burst(None, CAPTAIN, "ro")
    assert len(banner["dms"]) == 1


def test_an_every_connection_badge_names_the_archive_it_does_not_reach(banner):
    banner["rows"] = [WAIVER]
    assert "on every connection except `example-athena` (up to *RO*" in banner["text"]()


def test_an_archive_the_reader_cannot_query_is_not_named(banner):
    banner["rows"] = [WAIVER]
    banner["reach"] = {LEDGER.id}
    text = banner["text"]()
    assert "on every connection (up to *RO*" in text and "example-athena" not in text


def test_a_waiver_scoped_to_the_archive_is_not_promised(banner):
    banner["rows"] = [_scoped(ARCHIVE.id)]
    assert "Auto-approve active" not in banner["text"]()


def test_an_archive_that_turns_it_on_is_badged_like_any_other(banner):
    banner["fleet"] = [LEDGER, ARCHIVE_ON]
    banner["rows"] = [_scoped(ARCHIVE_ON.id)]
    assert "Auto-approve active* on `example-athena` (all dbs)" in banner["text"]()
    banner["rows"] = [WAIVER]
    assert "on every connection (up to *RO*" in banner["text"]()


# ---------------------------------------------------------------------------
# asking for a window
# ---------------------------------------------------------------------------

@pytest.fixture
def window(monkeypatch):
    from queryhub import auto_approve_requests as aar
    st = {"target": ARCHIVE, "made": [], "aar": aar}
    monkeypatch.setattr(cs, "kill_switch_on", lambda: False)
    monkeypatch.setattr(teams, "can_use_target", lambda pid, tid: True)
    monkeypatch.setattr(aar, "find_pending_for", lambda pid, tid: None)
    monkeypatch.setattr(targets, "get", lambda tid: st["target"])
    monkeypatch.setattr(aar, "create", lambda **kw: st["made"].append(kw) or {"id": 7})

    def go():
        return aar.submit_window(principal_id=CAPTAIN, name="Ex", target_id=st["target"].id,
                                 window_minutes=60, reason="reading the archive")
    st["go"] = go
    return st


def test_a_window_on_an_archive_is_refused(window):
    with pytest.raises(window["aar"].WindowRequestRefused) as e:
        window["go"]()
    assert (e.value.field, e.value.status) == ("target", 409)
    assert "always reviewed" in e.value.message
    assert window["made"] == []


def test_a_window_on_an_archive_that_turns_it_on_is_filed(window):
    window["target"] = ARCHIVE_ON
    row, _t = window["go"]()
    assert row == {"id": 7} and len(window["made"]) == 1


# ---------------------------------------------------------------------------
# the next caller
# ---------------------------------------------------------------------------

PACKAGE = Path(cs.__file__).resolve().parent
LOOKUPS = {"effective_grant", "fingerprint_cache_hit"}
# Every caller there is today. Listed so the scan cannot pass by finding
# nothing; a new caller does not need adding here, only to ask the question.
KNOWN = {
    "core_submit.validate_submission",
    "core_submit.create_request",
    "slack_app.handlers._maybe_dm_ro_burst",
    "slack_app.handlers.handle_batch_submission",
    "slack_app.modal._auto_approve_banner",
    "web.routes_data.connections",
    "web.routes_queries.classify_query",
    "web.routes_queries.submit_batch",
}


def _calls(node) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.attr if isinstance(f, ast.Attribute)
                    else f.id if isinstance(f, ast.Name) else "")
    return out


def _callers() -> dict[str, set[str]]:
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        module = ".".join(path.relative_to(PACKAGE).with_suffix("").parts)
        if module == "auto_approve":
            continue                     # where the two lookups live
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = _calls(fn)
                if calls & LOOKUPS:
                    found[f"{module}.{fn.name}"] = calls
    return found


def test_every_caller_of_the_lookups_asks_whether_auto_approve_is_allowed():
    """A waiver or a fingerprint match consulted without the question is a
    decision or a promise the archive rule does not reach. Read from the code,
    so the caller added next fails here rather than in production."""
    callers = _callers()
    assert KNOWN <= set(callers), f"the scan missed {sorted(KNOWN - set(callers))}"
    ungated = sorted(name for name, calls in callers.items()
                     if "auto_approve_allowed" not in calls)
    assert not ungated, (
        f"{ungated} consult a waiver or the fingerprint cache without asking "
        "engines.auto_approve_allowed(target) first")
