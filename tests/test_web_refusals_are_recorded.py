"""A statement the web refuses is kept in submission_failures, like Slack's.

Asked on 2026-09-26 why a requester's ClickHouse script was refused ("Schema
`system` is not queryable"), there was nothing to read: the Slack modal records
its refusals, the web did not, so the only traces were an empty draft row and a
422 in the access log. Now a web refusal is recorded with the SQL (passwords
masked), the connection, the database and the message. A confirmation prompt
is a question, not a refusal, and is not recorded.
"""
import json

from queryhub import core_submit
from queryhub.web import routes_queries

CLAIMS = {"sub": "U0EXAMPLE001", "name": "Jordan Ray"}


def _capture(monkeypatch):
    rows = []
    monkeypatch.setattr(routes_queries.db, "execute",
                        lambda sql, params=None: rows.append((sql, params)))
    return rows


def test_a_refusal_is_recorded_with_what_was_sent(monkeypatch):
    rows = _capture(monkeypatch)
    rej = core_submit.Rejection("query", "Schema `system` is not queryable on this engine.")
    routes_queries._record_refusal(CLAIMS, "web", 7, "ledger",
                                   "SELECT * FROM system.parts", rej)
    sql, params = rows[0]
    assert "INSERT INTO submission_failures" in sql
    assert params[:6] == ("U0EXAMPLE001", "Jordan Ray", "web", 7, "ledger",
                          "SELECT * FROM system.parts")
    assert json.loads(params[6])["web"].startswith("Schema `system`")


def test_a_password_in_a_refused_statement_is_masked(monkeypatch):
    rows = _capture(monkeypatch)
    rej = core_submit.Rejection("query", "refused")
    routes_queries._record_refusal(CLAIMS, "web", 7, "app",
                                   "CREATE ROLE x LOGIN PASSWORD 'hunter22'", rej)
    assert "hunter22" not in rows[0][1][5]


def test_a_confirmation_prompt_is_not_a_refusal(monkeypatch):
    rows = _capture(monkeypatch)
    rej = core_submit.Rejection("query", "confirm", reason="needs_confirmation",
                                reasons=("DROP cannot be undone",))
    routes_queries._record_refusal(CLAIMS, "web", 7, "app", "DROP TABLE t", rej)
    assert rows == []


def test_recording_never_changes_the_answer(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("metadata DB blinked")
    monkeypatch.setattr(routes_queries.db, "execute", broken)
    routes_queries._record_refusal(CLAIMS, "web", 7, "app", "SELECT 1",
                                   core_submit.Rejection("query", "refused"))


def test_both_web_submit_paths_record_before_refusing():
    import inspect
    single = inspect.getsource(routes_queries.submit_query)
    assert single.index("_record_refusal(") < single.index("_reject(prep)")
    assert "_record_refusal(claims, \"web-batch\"" in inspect.getsource(routes_queries.submit_batch)
