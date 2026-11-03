"""A second submit of a waiting statement answers with the waiting request.

A client that gave up on a slow call sends the same call again. Calls with
their own timeouts sit in front of QueryHub at the portal, so a retry after a
timeout is a normal event. The duplicate guard of `core_submit` refuses that
second submit, and the model then has no request id to poll. So `submit_query`
answers like a submit, for the request that already holds the statement, with
`duplicate: true`.

Nothing here may reach a database: the lookup is a fake that records its SQL.
"""
from types import SimpleNamespace

import pytest

from queryhub import db
from queryhub.core_submit import Rejection
from queryhub.mcp_server import policy, tools

ME = "U0EXAMPLE001"
PG = SimpleNamespace(id=7, alias="example-pg", engine="postgres", default_database="ledger")
PORTAL = "https://portal.example.test/queryhub"
DUPLICATE = Rejection("query", "You already have an active request (#41, status=pending) "
                               "with the same query on this target and database.",
                      reason="duplicate")


@pytest.fixture
def submit(monkeypatch):
    """`submit_query` up to the guard. `found` is the row that the lookup
    finds, `asked` the SQL and the parameters that it ran."""
    st = SimpleNamespace(found=None, asked=[], created=[], rejection=DUPLICATE, late=None)

    def fetch_one(sql, params=None):
        st.asked.append((" ".join(sql.split()), params))
        return st.found

    def refuse(*a, **k):
        raise AssertionError("a test in this module reached the database")
    for name in ("connection", "transaction", "execute", "fetch_all"):
        monkeypatch.setattr(db, name, refuse)
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(tools, "_me", lambda: ME)
    monkeypatch.setattr(tools, "_target_or_refuse", lambda uid, c: PG)
    monkeypatch.setattr(tools.caller, "display_name", lambda uid: "Ex")
    monkeypatch.setattr(tools.core_submit, "validate_submission",
                        lambda *a, **k: st.rejection or object())

    def create(prep):
        st.created.append(prep)
        return st.late or SimpleNamespace(auto_approved=False, row={"id": 50, "status": "pending"})
    monkeypatch.setattr(tools.core_submit, "create_request", create)
    monkeypatch.setattr(tools.core_submit, "dispatch_and_notify", lambda *a, **k: None)
    monkeypatch.setattr(policy, "max_tier", lambda: "ro")
    monkeypatch.setattr(policy.cfg, "get_setting",
                        lambda k, d=None: PORTAL if k == "mcp_portal_url" else d)
    return st


def _row(rid=41, status="pending", decided_by=None):
    return {"id": rid, "status": status, "required_tier": "ro",
            "decided_by_slack_id": decided_by}


def test_a_duplicate_answers_with_the_waiting_request(submit):
    submit.found = _row()
    out = tools.submit_query("example-pg", database="ledger", sql="  SELECT 1  ", justification="a check")
    assert out == {
        "requestId": 41, "requiredTier": "RO", "decision": "needs_approval",
        "status": "pending", "duplicate": True,
        "requestUrl": PORTAL + "?request=41",
        "note": ("You submitted this statement before. It is request 41. Do not "
                 "submit it again. Poll `queryhub_query_status` with this `requestId`."),
    }
    assert submit.created == [], "no second request"


def test_the_lookup_is_exact_and_belongs_to_the_person(submit):
    submit.found = _row()
    tools.submit_query("example-pg", database="ledger", sql="  SELECT 1  ", justification="a check")
    (sql, params), = submit.asked
    assert "requester_slack_id = %s" in sql and "target_server_id = %s" in sql
    assert "database_name = %s" in sql and "query = %s" in sql and "status = ANY(%s)" in sql
    assert "LIKE" not in sql.upper()
    # The database, and the statement as QueryHub stores it: stripped.
    assert params[:4] == (ME, 7, "ledger", "SELECT 1")
    assert params[4] == ["pending", "approved", "scheduled", "executing"]


@pytest.mark.parametrize("tier,first,second", [
    ("ro", "SELECT id FROM app_users WHERE password = 'a'",
     "SELECT id FROM app_users WHERE password = 'b'"),
    ("ddl", "ALTER ROLE app PASSWORD 'hunter1'", "ALTER ROLE app PASSWORD 'hunter2'"),
])
def test_a_statement_with_a_password_keeps_the_refusal(submit, monkeypatch, tier, first, second):
    """QueryHub stores a statement with its passwords masked, so two
    statements that differ only in a password have one stored form. The
    guard of `create_request` refuses the second. The stored form cannot say
    which one waits, so no answer hands over the request of the other."""
    monkeypatch.setattr(policy, "max_tier", lambda: tier)
    submit.found = _row()
    for sql in (first, second):
        with pytest.raises(tools.ToolError) as e:
            tools.submit_query("example-pg", database="ledger", sql=sql, justification="x")
        assert e.value.code == "duplicate" and e.value.message == DUPLICATE.message
    assert submit.asked == [], "no lookup for a statement with a password"


def test_a_race_with_a_password_keeps_the_refusal_too(submit):
    """The guard inside the transaction compares the masked text."""
    submit.rejection = None
    submit.late = DUPLICATE
    submit.found = _row(rid=43)
    with pytest.raises(tools.ToolError) as e:
        tools.submit_query("example-pg", database="ledger",
                           sql="SELECT id FROM app_users WHERE password = 'b'", justification="x")
    assert e.value.code == "duplicate" and submit.asked == []


@pytest.mark.parametrize("status,decided_by,decision", [
    ("pending", None, "needs_approval"),
    ("approved", "AUTO", "auto_approved"),
    ("executing", "AUTO", "auto_approved"),
    ("approved", "U0EXAMPLE009", "approved"),
    ("scheduled", "U0EXAMPLE009", "approved"),
])
def test_the_decision_says_who_approved_the_waiting_request(submit, status, decided_by, decision):
    submit.found = _row(status=status, decided_by=decided_by)
    out = tools.submit_query("example-pg", database="ledger", sql="SELECT 1", justification="a check")
    assert (out["status"], out["decision"], out["duplicate"]) == (status, decision, True)


def test_a_request_sent_back_for_changes_keeps_the_refusal(submit):
    """A DBA asked for another statement, so that request does not move
    again. The guard still refuses the same statement, and the lookup does
    not offer that request to poll."""
    with pytest.raises(tools.ToolError) as e:
        tools.submit_query("example-pg", database="ledger", sql="SELECT 1", justification="a check")
    (_, params), = submit.asked
    assert "changes_requested" not in params[4]
    assert e.value.code == "duplicate" and e.value.message == DUPLICATE.message


def test_without_the_waiting_request_the_refusal_stays(submit):
    """The request ended between the guard and the lookup."""
    with pytest.raises(tools.ToolError) as e:
        tools.submit_query("example-pg", database="ledger", sql="SELECT 1", justification="a check")
    assert e.value.code == "duplicate" and e.value.message == DUPLICATE.message


def test_a_race_inside_the_transaction_answers_the_same(submit):
    """create_request runs the guard again, under the lock of the person."""
    submit.rejection = None
    submit.late = DUPLICATE
    submit.found = _row(rid=43)
    out = tools.submit_query("example-pg", database="ledger", sql="SELECT 1", justification="a check")
    assert out["requestId"] == 43 and out["duplicate"] is True


def test_a_person_at_the_limit_gets_the_waiting_request_too(submit):
    """The limit of open requests runs before the guard."""
    submit.rejection = Rejection("rate_limit", "You already have 5 open request(s).")
    submit.found = _row()
    assert tools.submit_query("example-pg", database="ledger", sql="SELECT 1", justification="a check")["requestId"] == 41
    submit.found = None
    with pytest.raises(tools.ToolError) as e:
        tools.submit_query("example-pg", database="ledger", sql="SELECT 2", justification="a check")
    assert e.value.code == "rate_limit"


def test_another_refusal_does_not_look_for_a_request(submit):
    submit.rejection = Rejection("query", "Provide your SQL query.")
    with pytest.raises(tools.ToolError):
        tools.submit_query("example-pg", database="ledger", sql="SELECT 1", justification="a check")
    assert submit.asked == []


def test_a_new_statement_is_a_new_request(submit):
    submit.rejection = None
    out = tools.submit_query("example-pg", database="ledger", sql="SELECT 1", justification="a check")
    assert out["requestId"] == 50 and "duplicate" not in out
