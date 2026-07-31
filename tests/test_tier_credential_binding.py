"""The tier→credential binding, at the point of execution.

The core execution-trust invariant is that the credential actually used matches
the tier the query was classified and approved at. Existing tests prove the
executor *calls* get_credentials with some mode, because every one of them
monkeypatches it to `lambda *a, **k: ("u", "pw")` — a stub that returns the same
pair no matter what mode is asked for. So nothing proved the returned credential
corresponds to the requested tier, and nothing covered the CSV-import path,
which asks for DDL credentials unconditionally.

These tests use a credential store keyed BY MODE, so a mismatch is visible: an
RO query that ends up on the RW credential fails here instead of in production.
"""
import pytest

from queryhub import executor as ex

# Distinct credentials per tier, exactly as a real target has.
CREDS = {
    "ro":  ("app_ro",  "pw-ro"),
    "rw":  ("app_rw",  "pw-rw"),
    "ddl": ("app_ddl", "pw-ddl"),
}


def _target():
    return type("T", (), {"engine": "postgres", "alias": "svc", "id": 7,
                          "default_database": "app", "host": "h", "port": 5432})()


class _Cur:
    rowcount = 1

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return None


class _Txn:
    def __enter__(self):
        return _Cur()

    def __exit__(self, *a):
        return False


@pytest.fixture
def wired(monkeypatch):
    """Run _run far enough to fetch credentials, then stop.

    `used` records the (mode, user, password) triple the executor resolved, so
    the test can assert on the binding rather than on the call.
    """
    used = {}
    state = {"granted": "ddl", "classified": "ro"}

    monkeypatch.setattr(ex.targets, "get", lambda tid: _target())
    monkeypatch.setattr(ex.engines, "is_executable", lambda e: True)
    monkeypatch.setattr(ex.admins, "is_admin", lambda uid: False)
    monkeypatch.setattr(ex.requesters, "is_allowed", lambda uid: True)
    monkeypatch.setattr(ex.audit, "log_in", lambda *a, **k: None)
    monkeypatch.setattr(ex.row_limits, "effective_caps", lambda uid: (10, 10))
    monkeypatch.setattr(ex.db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(ex.db, "transaction", lambda: _Txn())
    monkeypatch.setattr(ex, "_fail",
                        lambda c, r, m: used.setdefault("fail", m))

    def _analyze(*a, **k):
        return type("R", (), {"blocked": False, "blockers": [],
                              "main_tier": state["classified"],
                              "statements": [1]})()
    monkeypatch.setattr(ex.query_safety, "analyze", _analyze)
    monkeypatch.setattr(ex.teams, "effective_mode_for_database",
                        lambda *a, **k: state["granted"])

    def _get_credentials(target_id, mode):
        # A real store: asking for the wrong tier gives the wrong credential,
        # which is the whole point of this file.
        if mode not in CREDS:
            raise LookupError(mode)
        used["mode"] = mode
        used["user"], used["password"] = CREDS[mode]
        return CREDS[mode]
    monkeypatch.setattr(ex.targets, "get_credentials", _get_credentials)

    # Stop right after the claim, before any real connection.
    def _stop(req):
        raise RuntimeError("stop after credentials")
    monkeypatch.setattr(ex, "_build_application_name", _stop)

    return used, state


def _req(query="SELECT 1"):
    return {"id": 1, "target_server_id": 7, "database_name": "app",
            "query": query, "requester_slack_id": "U0DEV"}


@pytest.mark.parametrize("tier", ["ro", "rw", "ddl"])
def test_credential_matches_the_classified_tier(wired, tier):
    """Whatever the query classifies as, that is the credential used — not the
    grant's ceiling, and not a cached value from an earlier statement."""
    used, state = wired
    state["classified"] = tier
    state["granted"] = "ddl"            # ceiling high enough for all three

    ex._run(_req(), client=None)

    assert used["mode"] == tier
    assert (used["user"], used["password"]) == CREDS[tier]


def test_a_read_never_runs_on_a_write_credential(wired):
    """The failure that matters: an RO-classified query on the RW/DDL
    credential means a mis-classification can write."""
    used, state = wired
    state["classified"] = "ro"
    state["granted"] = "ddl"

    ex._run(_req(), client=None)

    assert used["mode"] == "ro"
    assert used["user"] == "app_ro"
    assert used["password"] != CREDS["rw"][1]
    assert used["password"] != CREDS["ddl"][1]


def test_grant_ceiling_does_not_raise_the_credential_tier(wired):
    """A DDL-granted user running a plain SELECT still gets RO credentials.
    The grant is a ceiling, not a selection."""
    used, state = wired
    state["classified"] = "ro"
    state["granted"] = "ddl"
    ex._run(_req(), client=None)
    assert used["mode"] == "ro"


def test_tier_above_the_grant_never_reaches_the_credential_store(wired):
    """Re-authorization happens BEFORE the fetch: a query classified DDL for a
    user granted only RO must be refused without any credential being read."""
    used, state = wired
    state["classified"] = "ddl"
    state["granted"] = "ro"

    ex._run(_req("ALTER TABLE t ADD COLUMN c text"), client=None)

    assert "mode" not in used, "credentials fetched for an unauthorized tier"
    assert "fail" in used


def test_missing_credentials_for_the_required_tier_fail_closed(wired, monkeypatch):
    """A target with no DDL credential configured must refuse, not silently
    fall back to a lower tier that happens to exist."""
    used, state = wired
    state["classified"] = "ddl"
    state["granted"] = "ddl"

    def _only_ro(target_id, mode):
        if mode != "ro":
            raise LookupError(mode)
        return CREDS["ro"]
    monkeypatch.setattr(ex.targets, "get_credentials", _only_ro)

    ex._run(_req("ALTER TABLE t ADD COLUMN c text"), client=None)

    assert "fail" in used
    assert "DDL" in used["fail"] or "credentials" in used["fail"].lower()


def test_sentinel_password_is_refused(wired, monkeypatch):
    """The placeholder written by the target-sync job. Connecting with it would
    produce a confusing auth error against production instead of a clear
    "not configured yet"."""
    used, state = wired
    monkeypatch.setattr(ex.targets, "get_credentials",
                        lambda tid, mode: ("app_ro", ex._SENTINEL_PASSWORD))
    ex._run(_req(), client=None)
    assert "fail" in used


def test_csv_import_uses_ddl_credentials_deliberately(monkeypatch):
    """COPY needs table-level write, so the import path asks for DDL outright
    rather than deriving a tier. That is intentional — this test exists so the
    line is covered and the choice is visible, since it bypasses the
    classified-tier logic entirely."""
    seen = {}

    def _get_credentials(target_id, mode):
        seen["mode"] = mode
        return CREDS[mode]

    monkeypatch.setattr(ex.targets, "get", lambda tid: _target())
    monkeypatch.setattr(ex.engines, "is_executable", lambda e: True)
    monkeypatch.setattr(ex.targets, "get_credentials", _get_credentials)
    monkeypatch.setattr(ex, "_import_fail",
                        lambda c, i, m: seen.setdefault("fail", m))
    monkeypatch.setattr(ex.db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(ex.db, "transaction", lambda: _Txn())
    # The import path now re-authorizes the requester before fetching
    # credentials. This test's subject is WHICH TIER is asked for, so the
    # requester has to still be authorized: with `fetch_one` stubbed to None
    # above, `can_import` sees no import_grants row and refuses before the tier
    # is ever chosen.
    from queryhub import csv_import as _ci
    monkeypatch.setattr(_ci, "can_import", lambda uid: True)
    monkeypatch.setattr(ex.teams, "can_use_database", lambda u, t, d: True)
    imp = {"id": 5, "target_server_id": 7, "database_name": "app",
           "table_name": "t", "requester_slack_id": "U0DEV",
           "file_path": "/nonexistent.csv", "columns": ["a"], "delimiter": ","}
    # The import proceeds past credentials and then fails on the missing file /
    # absent connection, which is fine: the assertion is about which tier was
    # asked for, and _import_run's own handler catches the rest.
    ex._import_run(imp, client=None)

    assert seen.get("mode") == "ddl"
