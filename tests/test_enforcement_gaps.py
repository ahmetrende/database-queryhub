"""Two enforcement points that the main query path has and these did not.

The audit's framing was asymmetry, and asymmetry is the right way to read both:
each of these already has a correct implementation somewhere else in the
codebase, and one transport or one code path simply did not use it.

1. Schema browsing filtered by TARGET grant but not by the grant's
   `allowed_databases`. Reproduced on this deployment 2026-07-30 against a real
   restricted grant: `teams.can_use_database` returned False for a database
   outside the grant while `_resolve_database` returned it anyway. The WEB UI
   already gated the same view per database (`routes_data._granted_db`), so the
   two transports disagreed about who may see what.

2. A CSV import was neither re-authorized at execution time nor claimed
   atomically. It runs on DDL credentials and creates a table, which makes it
   the most privileged thing a non-admin can ask for; the main query path got
   both guards in B5 / F2.10 and this path got neither.
"""
import pytest


# ---------------------------------------------------------------------------
# 1. schema browsing is scoped per database
# ---------------------------------------------------------------------------

@pytest.fixture
def target():
    return type("T", (), {"id": 7, "alias": "svc-prod-example",
                          "default_database": "app"})()


def test_a_database_outside_the_grant_is_refused(monkeypatch, target):
    from queryhub.slack_app import subcommands as sc
    monkeypatch.setattr(sc.teams, "can_use_database",
                        lambda uid, tid, db: db == "app")
    monkeypatch.setattr(sc.teams, "allowed_databases_for_user",
                        lambda uid, tid: {"app"})
    # Snapshotted, so "no snapshot" cannot be the reason it is refused.
    monkeypatch.setattr(sc.schema_catalog, "list_snapshot_databases",
                        lambda tid: ["app", "other"])
    got, err = sc._resolve_database("U0EXAMPLE01", target, "other")
    assert got is None
    assert "does not include database `other`" in err


def test_the_refusal_lists_what_the_user_may_browse(monkeypatch, target):
    """A refusal with no way forward gets read as a broken tool."""
    from queryhub.slack_app import subcommands as sc
    monkeypatch.setattr(sc.teams, "can_use_database", lambda u, t, d: False)
    monkeypatch.setattr(sc.teams, "allowed_databases_for_user",
                        lambda u, t: {"app", "reporting"})
    monkeypatch.setattr(sc.schema_catalog, "list_snapshot_databases",
                        lambda tid: ["app", "reporting", "other"])
    _, err = sc._resolve_database("U0EXAMPLE01", target, "other")
    assert "`app`" in err and "`reporting`" in err


def test_a_database_inside_the_grant_still_resolves(monkeypatch, target):
    from queryhub.slack_app import subcommands as sc
    monkeypatch.setattr(sc.teams, "can_use_database",
                        lambda uid, tid, db: db == "app")
    monkeypatch.setattr(sc.schema_catalog, "list_snapshot_databases",
                        lambda tid: ["app", "other"])
    got, err = sc._resolve_database("U0EXAMPLE01", target, "app")
    assert (got, err) == ("app", None)


def test_an_unrestricted_grant_browses_everything(monkeypatch, target):
    """allowed_databases IS NULL means no restriction — admins and full grants
    must not be narrowed by this check."""
    from queryhub.slack_app import subcommands as sc
    monkeypatch.setattr(sc.teams, "can_use_database", lambda u, t, d: True)
    monkeypatch.setattr(sc.schema_catalog, "list_snapshot_databases",
                        lambda tid: ["app", "other", "third"])
    for dbname in ("app", "other", "third"):
        got, err = sc._resolve_database("U0EXAMPLE01", target, dbname)
        assert (got, err) == (dbname, None)


def test_the_default_database_is_checked_too(monkeypatch, target):
    """`/sql tables target` with no database uses target.default_database. If
    only the explicit form were checked, omitting the name would bypass it."""
    from queryhub.slack_app import subcommands as sc
    calls = []

    def _can(uid, tid, dbname):
        calls.append(dbname)
        return False
    monkeypatch.setattr(sc.teams, "can_use_database", _can)
    monkeypatch.setattr(sc.teams, "allowed_databases_for_user", lambda u, t: set())
    monkeypatch.setattr(sc.schema_catalog, "list_snapshot_databases",
                        lambda tid: ["app"])
    got, err = sc._resolve_database("U0EXAMPLE01", target, None)
    assert calls == ["app"]           # the default, not None
    assert got is None and err


def test_the_grant_check_uses_the_same_helper_as_submit():
    """Two copies of an authorization rule drift. This one calls the helper the
    submit path calls, so a change to the rule cannot leave the browser behind."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src"
           / "queryhub" / "slack_app" / "subcommands.py").read_text()
    assert "teams.can_use_database(user_id, target.id, database)" in src


# ---------------------------------------------------------------------------
# 2. CSV import: re-authorize, then claim exclusively
# ---------------------------------------------------------------------------

def _imp(**over):
    row = {"id": 5, "requester_slack_id": "U0EXAMPLE01", "requester_name": "n",
           "target_server_id": 7, "database_name": "app", "table_name": "t",
           "is_new_table": True, "unlogged": False, "delimiter": ",",
           "columns": ["a"], "row_count": 1, "csv_file_path": "/nonexistent",
           "column_defs": None, "status": "approved"}
    row.update(over)
    return row


@pytest.fixture
def import_env(monkeypatch):
    from queryhub import executor
    target = type("T", (), {"id": 7, "alias": "svc-prod-example",
                            "engine": "postgres"})()
    monkeypatch.setattr(executor.targets, "get", lambda tid: target)
    monkeypatch.setattr(executor.engines, "is_executable", lambda e: True)

    failures = []
    monkeypatch.setattr(executor, "_import_fail",
                        lambda client, imp, msg: failures.append(msg))

    # `_import_run` wraps its body in try/except, so a sentinel that RAISES is
    # swallowed and logged. Record the call instead, and raise the error the
    # code already knows how to report.
    reached = []

    def _no_creds(*a, **k):
        reached.append("credentials")
        raise LookupError("no ddl credentials in this test")
    monkeypatch.setattr(executor.targets, "get_credentials", _no_creds)
    return executor, failures, reached


def test_a_revoked_importer_does_not_get_their_import_run(import_env,
                                                          monkeypatch):
    executor, failures, reached = import_env
    from queryhub import csv_import
    monkeypatch.setattr(csv_import, "can_import", lambda uid: False)
    executor._import_run(_imp(), None)
    assert failures and "permission to import was removed" in failures[0]
    assert reached == [], "decrypted a DDL secret for a refused import"


def test_losing_access_to_the_database_stops_the_import(import_env,
                                                        monkeypatch):
    executor, failures, reached = import_env
    from queryhub import csv_import
    monkeypatch.setattr(csv_import, "can_import", lambda uid: True)
    monkeypatch.setattr(executor.teams, "can_use_database",
                        lambda uid, tid, dbname: False)
    executor._import_run(_imp(), None)
    assert failures and "was removed after this request was approved" in failures[0]
    assert reached == [], "decrypted a DDL secret for a refused import"


def test_a_still_authorized_import_gets_past_the_re_auth(import_env,
                                                         monkeypatch):
    """The other half of the argument: the guards must not reject everything."""
    executor, failures, reached = import_env
    from queryhub import csv_import
    monkeypatch.setattr(csv_import, "can_import", lambda uid: True)
    monkeypatch.setattr(executor.teams, "can_use_database",
                        lambda uid, tid, dbname: True)
    executor._import_run(_imp(), None)
    assert reached == ["credentials"], "the re-auth blocked an authorized import"
    assert failures and "no DDL credentials" in failures[0]


def test_the_import_claim_is_conditional_on_approved():
    """The UPDATE used to be unconditional, so the state flip recorded what
    happened instead of gating it: an import handed to the pool twice would COPY
    the same file twice, and a COPY into an existing table APPENDS."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src"
           / "queryhub" / "executor.py").read_text(encoding="utf-8")
    assert ("\"WHERE id=%s AND status='approved'\", (import_id,),") in src
    claim = src.index("UPDATE csv_imports SET status='executing'")
    guard = src.index("if cur.rowcount == 0:", claim)
    audit = src.index("import_execution_started", claim)
    assert guard < audit, "a lost claim must return before it audits a start"


def test_the_re_auth_runs_before_any_credential_is_fetched():
    """Fetching DDL credentials for a request that is about to be refused would
    decrypt a secret for nothing, and ordering is the only thing preventing it."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src"
           / "queryhub" / "executor.py").read_text(encoding="utf-8")
    run = src.index("def _import_run(")
    reauth = src.index("_ci_auth.can_import(requester)", run)
    creds = src.index('targets.get_credentials(target.id, "ddl")', run)
    assert reauth < creds
