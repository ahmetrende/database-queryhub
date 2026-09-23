"""A password in a submitted statement is never stored in cleartext.

Migration 100 masked `requests.query` when a request closed, so a role script's
password sat in the table for as long as the request was open, and on purpose
for one waiting on a DBA to run it by hand. The rule now is that it is not
stored at all: `query` is written masked, the executor gets the original from
an encrypted copy that exists only while the request can still run, and every
other table that keeps SQL -- saved workspaces, templates, favourites, refused
submissions -- keeps it masked.
"""
import ast
import inspect
from pathlib import Path

import pytest

from queryhub import (bundles, core_submit, executor, favorites, query_safety,
                           query_secrets, templates)
from queryhub.slack_app import handlers
from queryhub.web import routes_data

ROLE = "CREATE ROLE app LOGIN PASSWORD 'not-a-real-secret-42'"
MASKED = "CREATE ROLE app LOGIN PASSWORD '***REDACTED***'"
SRC = Path(__file__).resolve().parent.parent / "src" / "queryhub"
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION = (MIGRATIONS / "129_request_query_secret.sql").read_text(encoding="utf-8")


def _trigger_in_force() -> str:
    """The body of the requests_scrub_secrets() the last migration defined."""
    last = sorted(f for f in MIGRATIONS.glob("*.sql")
                  if "FUNCTION requests_scrub_secrets()" in f.read_text(encoding="utf-8"))[-1]
    sql = last.read_text(encoding="utf-8")
    start = sql.index("FUNCTION requests_scrub_secrets()")
    return sql[start:sql.index("LANGUAGE plpgsql", start)]


def _enum_members() -> set[str]:
    """request_status as the migrations build it: the CREATE TYPE, then every
    ADD VALUE after it."""
    import re
    names: set[str] = set()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        sql = f.read_text(encoding="utf-8")
        m = re.search(r"CREATE TYPE request_status AS ENUM \(([^)]*)\)", sql)
        if m:
            names |= set(re.findall(r"'([a-z_]+)'", m.group(1)))
        names |= set(re.findall(r"ALTER TYPE request_status ADD VALUE (?:IF NOT EXISTS )?'([a-z_]+)'", sql))
    return names


@pytest.fixture
def fernet(monkeypatch):
    monkeypatch.setattr(query_secrets.crypto, "encrypt", lambda s: "tok:" + s)
    monkeypatch.setattr(query_secrets.crypto, "decrypt", lambda t: t[len("tok:"):])


# --- what is stored ------------------------------------------------------------

def test_a_statement_with_a_password_is_stored_masked_and_kept_encrypted(fernet):
    assert query_secrets.split(ROLE) == (MASKED, "tok:" + ROLE)


def test_an_ordinary_statement_is_stored_as_written_with_no_secret(fernet):
    assert query_secrets.split("SELECT 1") == ("SELECT 1", None)


def test_every_insert_into_requests_goes_through_split():
    """A write path added later that stores `query` without splitting it would
    put the password back in the table. The draft row carries no SQL at all."""
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "INSERT INTO requests" not in text:
            continue
        for fn in ast.walk(ast.parse(text)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(text, fn) or ""
            if "INSERT INTO requests" not in body or fn.name == "reserve_request_id":
                continue
            assert "query_secrets.split(" in body and "query_secret" in body, (
                f"{path.name}:{fn.name} inserts a request without splitting its "
                f"query; a password in it would be stored in cleartext")


def test_the_trigger_masks_every_write_and_drops_the_copy_when_the_request_ends():
    assert "BEFORE INSERT OR UPDATE ON requests" in MIGRATION
    body = _trigger_in_force()
    # Masking is not gated on the status any more: no write stores a password.
    mask_at = body.index("NEW.query := scrub_query_secrets(NEW.query)")
    assert "IF NEW.status" not in body[:mask_at]
    for status in ("completed", "failed", "cancelled", "rejected", "expired",
                   "changes_requested", "awaiting_dba_manual"):
        assert f"'{status}'" in body[mask_at:], status
    assert "NEW.query_secret := NULL" in body


def test_the_trigger_cannot_fail_on_a_status_the_enum_lacks():
    """129 listed 'expired', which request_status does not have. An IN-list
    against an enum column casts each literal, so the trigger raised on EVERY
    insert and update of requests until 130 replaced it. Two guards: the
    comparison is on text, where an unknown name just never matches, and every
    name listed is a real member anyway."""
    import re
    body = _trigger_in_force()
    assert "NEW.status::text IN" in body
    listed = set(re.findall(r"'([a-z_]+)'", body[body.index("NEW.status::text IN"):]))
    unknown = listed - _enum_members() - {"expired"}   # named on purpose, see 130
    assert not unknown, f"not request_status members: {sorted(unknown)}"
    assert "awaiting_dba_manual" in _enum_members() and "expired" not in _enum_members()


def test_the_trigger_pattern_covers_escaped_quotes():
    """Migration 100's pattern stopped at the first quote, so `'it''s'` masked
    `'it'` and left the rest of the password behind."""
    fn = MIGRATION[MIGRATION.index("FUNCTION scrub_query_secrets"):MIGRATION.index("LANGUAGE sql")]
    assert "''''" in fn and "E''" in fn


# --- what is run ---------------------------------------------------------------

def test_the_executor_runs_the_original(monkeypatch, fernet):
    monkeypatch.setattr(query_secrets.db, "fetch_one",
                        lambda sql, p: {"query_secret": "tok:" + ROLE})
    assert query_secrets.statement_to_run({"id": 1, "query": MASKED}) == ROLE


def test_an_unmasked_statement_needs_no_second_read(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("read the row for a statement with nothing masked")
    monkeypatch.setattr(query_secrets.db, "fetch_one", boom)
    assert query_secrets.statement_to_run({"id": 1, "query": "SELECT 1"}) == "SELECT 1"


def test_a_masked_statement_whose_copy_is_gone_is_not_sent(monkeypatch):
    monkeypatch.setattr(query_secrets.db, "fetch_one", lambda sql, p: {"query_secret": None})
    with pytest.raises(query_secrets.SecretGone):
        query_secrets.statement_to_run({"id": 1, "query": MASKED})


def test_the_executor_fails_the_request_instead_of_setting_the_mask(monkeypatch):
    failed = []
    monkeypatch.setattr(executor.query_secrets, "statement_to_run",
                        lambda r: (_ for _ in ()).throw(query_secrets.SecretGone()))
    monkeypatch.setattr(executor, "_fail", lambda c, r, msg: failed.append(msg))
    executor._run({"id": 1, "query": MASKED, "target_server_id": 1}, None)
    assert failed and "Submit it again with the password" in failed[0]


def test_the_executor_analyses_the_statement_it_will_send(monkeypatch):
    seen = []

    class Stop(BaseException):
        """Escapes _run's own `except Exception`, so nothing after it runs."""

    def analyze(sql, **k):
        seen.append(sql)
        raise Stop()
    monkeypatch.setattr(executor.query_secrets, "statement_to_run", lambda r: ROLE)
    monkeypatch.setattr(executor.targets, "get", lambda tid: type(
        "T", (), {"enabled": True, "engine": "postgres", "alias": "a"})())
    monkeypatch.setattr(executor.engines, "is_executable", lambda e: True)
    monkeypatch.setattr(executor.admins, "is_super_admin", lambda uid: False)
    monkeypatch.setattr(executor.query_safety, "analyze", analyze)
    monkeypatch.setattr(executor, "_fail", lambda *a: None)
    monkeypatch.setattr(executor.log, "exception", lambda *a, **k: None)
    try:
        executor._run({"id": 1, "query": MASKED, "target_server_id": 1,
                       "requester_slack_id": "U0EXAMPLE001"}, None)
    except Stop:
        pass
    assert seen == [ROLE]


def test_a_statement_snippet_is_masked_before_it_is_cut():
    """Clamped first, the literal would lose its closing quote, stop matching,
    and the stored snippet would keep the password's first characters."""
    sql = "ALTER ROLE some_long_role_name_here WITH LOGIN PASSWORD 'abcdefghijklmnopqrstuvwxyz0123456789'"
    snip = executor._sql_snippet(sql)
    assert "abcdef" not in snip and len(snip) <= executor._SNIPPET_CHARS


# --- resubmitting what QueryHub shows ------------------------------------------

def test_a_copied_back_statement_is_refused_with_a_reason(monkeypatch):
    from queryhub import lifecycle
    # Process-global, and another test can leave it set.
    monkeypatch.setattr(lifecycle, "is_draining", lambda: False)
    monkeypatch.setattr(core_submit, "kill_switch_on", lambda: False)
    monkeypatch.setattr(core_submit.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(core_submit.cfg, "get_int", lambda k, d=None: d)
    r = core_submit.validate_submission("U0EXAMPLE001", "Ex", target_server_id=1,
                                        database_name="main", query=MASKED,
                                        justification="new role")
    assert isinstance(r, core_submit.Rejection) and r.field == "query"
    assert "Type the password in again" in r.message


def test_the_mask_is_recognised_in_each_form_it_takes():
    assert query_safety.has_masked_password(MASKED)
    assert query_safety.has_masked_password("CREATE LOGIN a WITH PASSWORD = '***REDACTED***'")
    assert not query_safety.has_masked_password(ROLE)
    assert not query_safety.has_masked_password("SELECT '***REDACTED***'")


# --- the other tables that keep SQL ----------------------------------------------

def test_a_template_is_saved_masked(monkeypatch):
    seen = []
    monkeypatch.setattr(templates.db, "insert_returning", lambda sql, p: seen.append(p) or {})
    templates.save(owner_slack_id="U0EXAMPLE001", name="t", query=ROLE,
                   target_server_id=None, database_name=None)
    assert MASKED in seen[0] and ROLE not in seen[0]


def test_a_favourite_is_saved_masked(monkeypatch):
    seen = []
    monkeypatch.setattr(favorites.db, "insert_returning", lambda sql, p: seen.append(p) or {})
    monkeypatch.setattr(favorites, "_trim", lambda pid: None)
    favorites.add(principal_id="U0EXAMPLE001", query=ROLE, target_server_id=None,
                  database_name=None)
    assert MASKED in seen[0]


def test_a_saved_workspace_is_saved_masked(monkeypatch):
    seen = []
    monkeypatch.setattr(routes_data.deps, "require_whitelisted", lambda c: None)
    monkeypatch.setattr(routes_data.db, "fetch_one", lambda sql, p: seen.append(p) or None)
    monkeypatch.setattr(routes_data.mapping, "session_entry", lambda r: r)
    routes_data.sessions_upsert(routes_data.SessionIn(name="w", tabs=[{"sql": ROLE}]),
                                {"sub": "U0EXAMPLE001"})
    assert "not-a-real-secret" not in seen[0][2] and "REDACTED" in seen[0][2]


def test_a_refused_submission_is_logged_masked():
    src = inspect.getsource(handlers)
    i = src.index("INSERT INTO submission_failures")
    assert "mask_password_literals(query)" in src[i:i + 600]


def test_a_batch_item_is_split_like_a_single_request():
    src = inspect.getsource(bundles)
    assert "query_secrets.split(item[\"query\"])" in src
