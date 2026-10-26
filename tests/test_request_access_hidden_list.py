"""The request-access forms with the connection list OFF, which is the default.

A list of every connection shows each person on the whitelist the name and the
host of every database. With `request_access_list_enabled` off, no surface
lists them: the Slack modal and the QueryHub Web form (and so the IdP panel,
which forwards to the same routes) ask for the server and the database as text.

What these tests pin:

* `GET /requestable` and the Slack target options say nothing about the fleet.
* The typed server resolves only on an exact alias. The requester gets the same
  answer whether it resolved or not, so the form cannot be used to test names.
* `connectionId` is ignored, so the old picked route is no way to test names.
* A typed name for the control plane stays free text. It never raises.
"""
import pytest
from fastapi import HTTPException

from queryhub import access_requests, idp_outbox
from queryhub.slack_app import access, handlers
from queryhub.web import routes_requests as rr


_REAL_RESOLVE = access_requests.resolve_typed_server   # before any test patches it


class _T:
    def __init__(self, tid=7, alias="prod-main", enabled=True):
        self.id = tid
        self.alias = alias
        self.enabled = enabled
        self.host = "prod-main.internal.example"
        self.port = 5432
        self.default_database = "payments"


# --------------------------------------------------------------------------
# resolve_typed_server
# --------------------------------------------------------------------------

@pytest.fixture
def lookup(monkeypatch):
    """resolve_typed_server with the database row and the control plane stubbed."""
    seen = {"sql": None, "args": None, "got": None}

    def _wire(row, control_plane=frozenset(), target=None):
        def fetch_one(sql, args=()):
            seen["sql"], seen["args"] = sql, args
            return row
        monkeypatch.setattr(access_requests.db, "fetch_one", fetch_one)
        from queryhub import grants, targets
        monkeypatch.setattr(grants, "control_plane_target_ids", lambda: set(control_plane))
        monkeypatch.setattr(targets, "get", lambda tid: seen.update(got=tid) or target)
        return seen
    return _wire


def test_a_blank_text_never_reaches_the_database(monkeypatch):
    monkeypatch.setattr(access_requests.db, "fetch_one",
                        lambda *a, **k: pytest.fail("a blank name must not query"))
    assert access_requests.resolve_typed_server("   ") is None
    assert access_requests.resolve_typed_server(None) is None


def test_the_match_is_exact_enabled_and_not_deleted(lookup):
    seen = lookup(row=None)
    assert access_requests.resolve_typed_server("  Prod-Main ") is None
    assert seen["args"] == ("Prod-Main",)              # trimmed, case left to the SQL
    sql = " ".join(seen["sql"].split())
    assert "lower(alias) = lower(%s)" in sql           # exact, ignoring case
    assert "AND enabled" in sql and "deleted_at IS NULL" in sql
    assert "LIKE" not in sql.upper() and "ILIKE" not in sql.upper()   # no partial match


def test_an_exact_alias_resolves(lookup):
    t = _T()
    seen = lookup(row={"id": 7}, target=t)
    assert access_requests.resolve_typed_server("prod-main") is t
    assert seen["got"] == 7


def test_the_control_plane_never_resolves(lookup):
    lookup(row={"id": 1}, control_plane={1}, target=_T(1, "bot-control"))
    assert access_requests.resolve_typed_server("bot-control") is None


def test_free_text_key_is_the_shape_the_idp_outbox_reads_back():
    key = access_requests.free_text_key("payment-svc", None, "ledger")
    row = {"attempted_query": key, "requested_tier": None, "database_name": "ledger"}
    assert idp_outbox._access_target(row, None) == "payment-svc"
    web = {"attempted_query": access_requests.free_text_key("payment-svc", "ro", "ledger"),
           "requested_tier": "ro", "database_name": "ledger"}
    assert idp_outbox._access_target(web, None) == "payment-svc"


# --------------------------------------------------------------------------
# the web routes
# --------------------------------------------------------------------------

def test_requestable_is_empty_and_unlisted_when_the_list_is_off(monkeypatch):
    monkeypatch.setattr(rr.deps, "require_whitelisted", lambda c: None)
    monkeypatch.setattr(rr.access_requests, "list_visible", lambda: False)
    monkeypatch.setattr(rr.targets, "list_enabled",
                        lambda: pytest.fail("the fleet must not be read"))
    monkeypatch.setattr(rr.teams, "effective_grant_for_user",
                        lambda *a: pytest.fail("no grant lookup either"))
    assert rr.requestable(claims={"sub": "U1"}) == {"connections": [], "listed": False}


def test_requestable_says_listed_when_the_list_is_on(monkeypatch):
    monkeypatch.setattr(rr.deps, "require_whitelisted", lambda c: None)
    monkeypatch.setattr(rr.access_requests, "list_visible", lambda: True)
    monkeypatch.setattr(rr.targets, "list_enabled", lambda: [])
    monkeypatch.setattr(rr.grants, "control_plane_target_ids", lambda: set())
    assert rr.requestable(claims={"sub": "U1"}) == {"connections": [], "listed": True}


@pytest.fixture
def web(monkeypatch):
    """endpoint_request with the list OFF. Anything the picked route would call
    fails the test."""
    state = {"created": None, "resolved": [], "calls": []}

    def _wire(known=None):
        monkeypatch.setattr(rr.deps, "require_whitelisted", lambda c: None)
        monkeypatch.setattr(rr.access_requests, "list_visible", lambda: False)
        monkeypatch.setattr(rr, "_target_by_alias",
                            lambda a: pytest.fail("the picked route must not run"))
        monkeypatch.setattr(rr, "_target_by_id",
                            lambda v: pytest.fail("the picked route must not run"))
        monkeypatch.setattr(rr, "_catalog_databases",
                            lambda t: pytest.fail("the catalogue must not be read"))

        def resolve(text):
            state["resolved"].append(text)
            return known if known and text.lower() == known.alias else None
        monkeypatch.setattr(rr.access_requests, "resolve_typed_server", resolve)

        def create(**kw):
            state["created"] = kw
            state["calls"].append(kw)
            return {"id": 42, **kw}
        monkeypatch.setattr(rr.access_requests, "create", create)
        monkeypatch.setattr(rr.access_requests, "open_count_for", lambda u: 0)
        monkeypatch.setattr(rr, "_bot_client", lambda: None)
        import queryhub.admins as admins_mod
        monkeypatch.setattr(admins_mod, "is_admin", lambda u: False)
        monkeypatch.setattr(admins_mod, "list_active", lambda: [{"id": "UA"}])
        monkeypatch.setattr(rr.access_requests, "notify_admins", lambda *a, **k: "1.0")
        return state
    return _wire


def _body(**kw):
    base = {"server": "prod-main", "database": "payments", "tier": "RO",
            "reason": "reconciliation report for finance"}
    base.update(kw)
    return rr.EndpointRequestIn(**base)


def test_a_typed_alias_is_not_looked_up_when_the_list_is_off(web):
    # A lookup would make a name that exists differ from one that does not.
    state = web(known=_T())
    out = rr.endpoint_request(_body(server="Prod-Main"), claims={"sub": "U1"})
    assert out["status"] == "submitted"
    assert state["resolved"] == []
    assert state["created"]["target_server_id"] is None
    assert state["created"]["reason"].startswith("[server: Prod-Main] ")
    assert state["created"]["attempted_query"].startswith(access_requests.FREE_TEXT_MARK)


def test_any_other_text_stays_free_text(web):
    state = web(known=_T())
    out = rr.endpoint_request(_body(server="prod-mian"), claims={"sub": "U1"})
    assert out["status"] == "submitted"
    assert state["created"]["target_server_id"] is None
    assert state["created"]["reason"].startswith("[server: prod-mian] ")
    assert state["created"]["attempted_query"].startswith(access_requests.FREE_TEXT_MARK)


def test_the_answer_is_the_same_for_a_name_that_exists_and_one_that_does_not(web):
    state = web(known=_T())
    hit = rr.endpoint_request(_body(server="prod-main"), claims={"sub": "U1"})
    miss = rr.endpoint_request(_body(server="no-such-server"), claims={"sub": "U1"})
    assert set(hit) == set(miss)
    assert hit["status"] == miss["status"] == "submitted"
    assert hit["slackMessageTs"] == miss["slackMessageTs"]
    assert state["resolved"] == []
    # What is stored differs only by the typed name.
    a, b = state["calls"]
    assert a["target_server_id"] is b["target_server_id"] is None
    def norm(c, n):
        return {k: (v.replace(n, "<name>") if isinstance(v, str) else v)
                for k, v in c.items()}
    assert norm(a, "prod-main") == norm(b, "no-such-server")


def test_the_dedupe_answer_is_the_same_for_a_name_that_exists_and_one_that_does_not(
        monkeypatch, web):
    """The unique index ignores the tier and the database of a RESOLVED request
    (target set, no query), so the same name with another tier gave 409 for a
    name that exists and 201 for one that does not. Stored as text, both give
    the same answer. The fake below keys like the index does."""
    web(known=_T())
    seen = set()

    def create(**kw):
        key = (kw["principal_id"], kw["target_server_id"],
               kw["attempted_query"] or "")
        if key in seen:
            return None
        seen.add(key)
        return {"id": 42, **kw}
    monkeypatch.setattr(rr.access_requests, "create", create)

    def probe(name):
        out = []
        for tier, db in (("RO", "payments"), ("RW", "ledger")):
            try:
                out.append(rr.endpoint_request(_body(server=name, tier=tier, database=db),
                                               claims={"sub": "U1"})["status"])
            except HTTPException as e:
                out.append(e.status_code)
        return out

    assert probe("prod-main") == probe("no-such-server") == ["submitted", "submitted"]
    # The same request twice is still a conflict, for both.
    assert probe("prod-main") == probe("no-such-server") == [409, 409]


def test_connection_id_is_ignored_when_the_list_is_off(web):
    # With the picked route a made-up id is a 404 and a real one a 201. Ignoring
    # it removes that difference. The text in `server` decides.
    state = web(known=_T())
    out = rr.endpoint_request(_body(server="whatever", connectionId="ghost"),
                              claims={"sub": "U1"})
    assert out["status"] == "submitted"
    assert state["resolved"] == []
    assert state["created"]["target_server_id"] is None


def test_a_typed_control_plane_name_is_free_text_and_never_raises(monkeypatch, web):
    state = web()
    monkeypatch.setattr(rr.access_requests, "resolve_typed_server", _REAL_RESOLVE)
    monkeypatch.setattr(access_requests.db, "fetch_one",
                        lambda *a, **k: pytest.fail("a typed name must not be looked up"))
    out = rr.endpoint_request(_body(server="bot-control"), claims={"sub": "U1"})
    assert out["status"] == "submitted"
    assert state["created"]["target_server_id"] is None


def test_with_the_list_on_a_typed_alias_still_resolves(monkeypatch, web):
    # The requester already sees every name, so one click may approve it.
    state = web(known=_T())
    monkeypatch.setattr(rr.access_requests, "list_visible", lambda: True)
    out = rr.endpoint_request(_body(server="prod-main"), claims={"sub": "U1"})
    assert out["status"] == "submitted"
    assert state["resolved"] == ["prod-main"]
    assert state["created"]["target_server_id"] == 7
    assert state["created"]["attempted_query"] is None


# --------------------------------------------------------------------------
# the Slack modal
# --------------------------------------------------------------------------

def _target_block(view):
    return next(b for b in view["blocks"] if b.get("block_id") == access.B_TARGET)


def test_the_modal_asks_for_text_when_the_list_is_off():
    view = access.build_request_modal(listed=False)
    el = _target_block(view)["element"]
    assert el["type"] == "plain_text_input" and el["action_id"] == access.A_TARGET
    assert "external_select" not in repr(view)


def test_the_modal_keeps_the_search_box_when_the_list_is_on():
    el = _target_block(access.build_request_modal(listed=True))["element"]
    assert el["type"] == "external_select"


def test_the_modal_follows_the_setting_by_default(monkeypatch):
    monkeypatch.setattr(access_requests, "list_visible", lambda: False)
    assert _target_block(access.build_request_modal())["element"]["type"] == "plain_text_input"
    monkeypatch.setattr(access_requests, "list_visible", lambda: True)
    assert _target_block(access.build_request_modal())["element"]["type"] == "external_select"


def test_the_target_options_are_empty_when_the_list_is_off(monkeypatch):
    monkeypatch.setattr(access_requests, "list_visible", lambda: False)
    monkeypatch.setattr(access.targets, "list_enabled",
                        lambda: pytest.fail("the fleet must not be read"))
    monkeypatch.setattr(access.targets, "search",
                        lambda q: pytest.fail("the fleet must not be searched"))
    assert access.options_for_targets("") == []
    assert access.options_for_targets("prod") == []


def _state(target, database="", query="", reason="because I need it"):
    return {"values": {
        access.B_TARGET: {access.A_TARGET: target},
        access.B_DATABASE: {access.A_DATABASE: {"value": database}},
        access.B_QUERY: {access.A_QUERY: {"value": query}},
        access.B_REASON: {access.A_REASON: {"value": reason}},
    }}


def test_a_typed_target_is_read_as_text():
    parsed = access.parse_modal_submission(
        _state({"type": "plain_text_input", "value": "  Payment-Svc "}, database="ledger"))
    assert parsed["server_text"] == "Payment-Svc"
    assert parsed["target_server_id"] is None
    assert parsed["database_name"] == "ledger"


def test_a_picked_target_is_still_read_as_an_id():
    parsed = access.parse_modal_submission(
        _state({"type": "external_select", "selected_option": {"value": "7"}}))
    assert parsed["target_server_id"] == 7
    assert parsed["server_text"] is None


# --------------------------------------------------------------------------
# the Slack submission
# --------------------------------------------------------------------------

@pytest.fixture
def slack(monkeypatch):
    """handle_access_request_submission with everything outside it stubbed."""
    state = {"ack": [], "created": None, "pending_args": None, "fan_out": None, "dms": []}

    def _wire(resolved=None):
        monkeypatch.setattr(handlers, "_kill_switch_on", lambda: False)
        monkeypatch.setattr(handlers.access_requests, "resolve_typed_server",
                            lambda text: pytest.fail("a typed name must not be looked up"))

        def pending(uid, tid, query):
            state["pending_args"] = (uid, tid, query)
            return None
        monkeypatch.setattr(handlers.access_requests, "find_pending_for", pending)

        def create(**kw):
            state["created"] = kw
            return {"id": 9, "requester_slack_id": kw["principal_id"],
                    "target_server_id": kw["target_server_id"],
                    "database_name": kw["database_name"],
                    "attempted_query": kw["attempted_query"], "reason": kw["reason"]}
        monkeypatch.setattr(handlers.access_requests, "create", create)
        monkeypatch.setattr(handlers.admins, "list_active",
                            lambda: [{"slack_user_id": "UA"}])
        monkeypatch.setattr(handlers.access, "fan_out_admin_dms",
                            lambda client, row, target, requested_server=None:
                            state.update(fan_out=(target, requested_server)))
        monkeypatch.setattr(handlers.notifications, "dm_requester",
                            lambda client, uid, text: state["dms"].append(text))
        monkeypatch.setattr(handlers.targets, "get",
                            lambda tid: resolved if resolved and tid == resolved.id else None)
        return state

    def _run(state, target_state, **kw):
        body = {"user": {"id": "U1", "name": "dev"},
                "view": {"state": _state(target_state, **kw)}}
        handlers.handle_access_request_submission(
            lambda *a, **k: state["ack"].append((a, k)), body, object())
    return _wire, _run


TYPED = {"type": "plain_text_input", "value": "payment-svc"}


def test_slack_typed_unknown_server_is_saved_as_free_text(slack):
    wire, run = slack
    state = wire(resolved=None)
    run(state, TYPED, database="ledger")
    c = state["created"]
    assert c["target_server_id"] is None
    assert c["reason"] == "[server: payment-svc] because I need it"
    assert c["attempted_query"] == access_requests.free_text_key("payment-svc", None, "ledger")
    assert state["fan_out"] == (None, "payment-svc")
    assert "payment-svc" in state["dms"][-1] and "`?`" not in state["dms"][-1]
    assert state["pending_args"][1] is None


def test_slack_a_typed_alias_is_stored_like_any_other_typed_name(slack):
    # The fixture fails the test if the text is looked up. A name that exists
    # and one that does not leave the same trace.
    wire, run = slack
    state = wire(resolved=_T())
    run(state, {"type": "plain_text_input", "value": "Prod-Main"}, database="payments")
    c = state["created"]
    assert c["target_server_id"] is None
    assert c["reason"] == "[server: Prod-Main] because I need it"
    assert c["attempted_query"] == access_requests.free_text_key("Prod-Main", None, "payments")
    assert state["fan_out"] == (None, "Prod-Main")
    assert state["pending_args"][1] is None


def test_slack_a_written_query_is_kept_for_an_unknown_server(slack):
    wire, run = slack
    state = wire(resolved=None)
    run(state, TYPED, query="SELECT 1")
    assert state["created"]["attempted_query"] == "SELECT 1"


def test_slack_a_blank_typed_server_is_refused(slack):
    wire, run = slack
    state = wire(resolved=None)
    run(state, {"type": "plain_text_input", "value": "   "})
    (args, kwargs) = state["ack"][0]
    assert kwargs == {} and args[0]["errors"] == {access.B_TARGET: "Enter the server name."}
    assert state["created"] is None
