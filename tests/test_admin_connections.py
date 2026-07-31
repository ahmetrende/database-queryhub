"""Connection CRUD in the web admin — the registry is now editable from a
browser, so the two properties that used to be guaranteed by "only an operator
with psql can do this" have to be guaranteed by code.

The first is AUTHORIZATION. Registering a target is how a database enters
QueryHub's blast radius, and rotating its credential is how someone takes it
over; both are super-admin work. A scoped "dba" admin can approve queries all
day and must still be refused here — and that is a different check from the
"not an admin at all" case the router-wide test in test_admin_routes_gated.py
already covers.

The second is that a DELETE never rewrites history. `requests` holds the record
of what ran where, and the grant tables hold live access; a target with either
is disabled instead of deleted, because the alternative is a delete that
silently cascades six people's access away or leaves an audit trail pointing at
a row that no longer exists.

Routes are called directly with fake claims — no TestClient, no DB.
"""
import inspect

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from queryhub import crypto, targets
from queryhub.web import admin as web_admin
from queryhub.web import routes_admin as ra

SUPER = {"sub": "U000EXAMPLE", "name": "Super Admin"}
DBA = {"sub": "U000SCOPED1", "name": "Scoped Dba"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeCursor:
    """Records every statement and hands back a fixed RETURNING row."""

    def __init__(self, returning=None):
        self.calls = []
        self._returning = returning or {"id": 77}

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._returning

    def statements(self, needle):
        return [c for c in self.calls if needle in c[0]]


class FakeTxn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


def _row(**over):
    """An admin_row() shaped dict — what every route reads a target through."""
    row = {
        "id": 42, "alias": "prod-beta", "host": "db.example.internal",
        "port": 5432, "default_database": "ledger", "enabled": True,
        "notes": None, "engine": "postgres", "secrets_provider": "local",
        "credentials": {
            "ro": {"username": "queryhub_ro", "configured": True,
                   "placeholder": False},
            "rw": {"username": None, "configured": False, "placeholder": False},
            "ddl": {"username": None, "configured": False, "placeholder": False},
        },
    }
    row.update(over)
    return row


NO_REFS = {"requests": 0, "csv_imports": 0, "user_grants": 0, "team_grants": 0,
           "auto_grants": 0, "access_requests": 0, "schema_tables": 0}


@pytest.fixture
def wire(monkeypatch):
    """Super-admin session, one registered target, no references, and a cursor
    standing in for the transaction. Tests reach into `state` to vary it."""
    state = {"row": _row(), "refs": dict(NO_REFS), "cur": FakeCursor(),
             "audit": [], "super": True}

    monkeypatch.setattr(web_admin.admins, "is_admin", lambda uid: True)
    monkeypatch.setattr(web_admin.admins, "is_super_admin",
                        lambda uid: state["super"])
    monkeypatch.setattr(ra.targets, "by_alias",
                        lambda alias: (type("T", (), {"id": state["row"]["id"]})()
                                       if alias == state["row"]["alias"] else None))
    monkeypatch.setattr(ra.targets, "admin_row", lambda tid: state["row"])
    monkeypatch.setattr(ra.targets, "list_admin_rows", lambda: [state["row"]])
    monkeypatch.setattr(ra.targets, "reference_counts", lambda tid: state["refs"])
    monkeypatch.setattr(ra.db, "transaction", lambda: FakeTxn(state["cur"]))
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        state["audit"].append((action, details)))
    from queryhub.web import routes_data
    monkeypatch.setattr(routes_data, "_catalog_databases", lambda tid: ["ledger"])
    return state


# ---------------------------------------------------------------------------
# Authorization — super-admin only, on every mutation
# ---------------------------------------------------------------------------

def _mutations():
    """(name, callable) for every route that changes or probes the registry."""
    return [
        ("create", lambda: ra.admin_create_connection(
            ra.ConnectionIn(alias="alpha-svc", host="db.example.internal",
                            defaultDatabase="ledger"), claims=DBA)),
        ("update", lambda: ra.admin_update_connection(
            "prod-beta", ra.ConnectionPatch(enabled=False), claims=DBA)),
        ("delete", lambda: ra.admin_delete_connection("prod-beta", claims=DBA)),
        ("test-new", lambda: ra.admin_test_new_connection(
            ra.ConnectionTestIn(host="db.example.internal",
                                defaultDatabase="ledger",
                                username="reader", password="s3cret"),
            claims=DBA)),
        ("test-stored", lambda: ra.admin_test_connection("prod-beta", claims=DBA)),
        ("list", lambda: ra.admin_connections(claims=DBA)),
    ]


@pytest.mark.parametrize("name,call", _mutations(), ids=[n for n, _ in _mutations()])
def test_a_scoped_admin_cannot_touch_the_registry(wire, name, call):
    """An admin who is not a super-admin is refused. This is the case the
    router-wide gate test cannot see: it drives a plain non-admin, so a route
    that asked for need="review" instead of need="access" would pass there and
    hand a query approver the ability to re-point a production target."""
    wire["super"] = False
    with pytest.raises(HTTPException) as e:
        call()
    assert e.value.status_code == 403
    assert e.value.detail["code"] == "forbidden"
    assert "Super-admin" in e.value.detail["message"]


def test_the_authorization_check_is_not_vacuous(wire, monkeypatch):
    """A super-admin gets through — otherwise every assertion above would hold
    for a route that is simply broken."""
    monkeypatch.setattr(ra.targets, "get_credentials",
                        lambda tid, mode: ("reader", "s3cret"))
    monkeypatch.setattr(ra, "_probe", lambda *a, **k: {"ok": True})
    assert ra.admin_connections(claims=SUPER)["connections"][0]["id"] == "prod-beta"
    assert ra.admin_test_connection("prod-beta", claims=SUPER)["ok"] is True


def test_an_unknown_connection_is_a_404_not_a_crash(wire):
    for call in (lambda: ra.admin_update_connection(
                     "nope", ra.ConnectionPatch(notes="x"), claims=SUPER),
                 lambda: ra.admin_delete_connection("nope", claims=SUPER),
                 lambda: ra.admin_test_connection("nope", claims=SUPER)):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 404


# ---------------------------------------------------------------------------
# DELETE — never rewrite history, never silently drop access
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref,count", [
    ("requests", 3),          # query history
    ("csv_imports", 1),       # import history
    ("user_grants", 1),       # live per-user access
    ("team_grants", 2),       # live team access
    ("auto_grants", 1),       # live auto-approve window
])
def test_delete_is_refused_and_the_target_is_disabled_instead(
        wire, monkeypatch, ref, count):
    deleted = []
    monkeypatch.setattr(ra.targets, "delete_in",
                        lambda cur, tid: deleted.append(tid))
    updates = []
    monkeypatch.setattr(ra.targets, "update_in",
                        lambda cur, tid, changes: updates.append(changes) or [])
    wire["refs"][ref] = count

    out = ra.admin_delete_connection("prod-beta", claims=SUPER)

    assert out["deleted"] is False and out["disabled"] is True
    assert deleted == [], "a referenced target must not be deleted"
    assert updates == [{"enabled": False}], "...but it must be disabled"
    # The operator has to be told which reference blocked them, or "it was
    # disabled instead" is an unexplained refusal.
    assert ref.replace("_", " ") in out["reason"]
    assert str(count) in out["reason"]
    action, details = wire["audit"][-1]
    assert action == "connection_updated"
    assert details["references"] == {ref: count}


def test_delete_removes_a_connection_nothing_points_at(wire, monkeypatch):
    deleted = []
    monkeypatch.setattr(ra.targets, "delete_in",
                        lambda cur, tid: deleted.append(tid))
    out = ra.admin_delete_connection("prod-beta", claims=SUPER)
    assert out["deleted"] is True and out["disabled"] is False
    assert deleted == [42]
    assert wire["audit"][-1][0] == "connection_deleted"


def test_a_catalog_snapshot_and_old_access_requests_do_not_block_a_delete(
        wire, monkeypatch):
    """schema_tables is a cache of the target's own catalog and access_requests
    null out by design, so neither is history worth keeping the row for. Only
    the five blocking kinds above should refuse."""
    deleted = []
    monkeypatch.setattr(ra.targets, "delete_in",
                        lambda cur, tid: deleted.append(tid))
    wire["refs"].update({"schema_tables": 900, "access_requests": 4})
    assert ra.admin_delete_connection("prod-beta", claims=SUPER)["deleted"] is True
    assert deleted == [42]


def test_delete_drops_the_catalog_before_the_target(wire):
    """schema_tables holds a restricting foreign key, so the real delete_in has
    to clear it first or the statement errors on any target that was ever
    snapshotted."""
    cur = FakeCursor()
    targets.delete_in(cur, 42)
    order = [sql for sql, _ in cur.calls]
    assert "DELETE FROM schema_tables" in order[0]
    assert "DELETE FROM target_servers" in order[1]


# ---------------------------------------------------------------------------
# CREATE — disabled, and never a plaintext password
# ---------------------------------------------------------------------------

@pytest.fixture
def keyring(tmp_path, monkeypatch):
    """A real Fernet key, so the encryption assertions test encryption rather
    than a stubbed-out stand-in for it."""
    path = tmp_path / "master.key"
    path.write_bytes(Fernet.generate_key() + b"\n")
    path.chmod(0o600)
    monkeypatch.setenv("MASTER_KEY_PATH", str(path))
    crypto.reset_cache()
    yield
    crypto.reset_cache()


def test_create_in_has_no_way_to_ask_for_an_enabled_target():
    """Structural, not behavioural: the policy is that a new target starts
    disabled, and the strongest form of that is a function with no parameter
    for the other answer. A future caller cannot pass enabled=True by mistake
    because there is nothing to pass."""
    assert "enabled" not in inspect.signature(targets.create_in).parameters


def test_create_writes_enabled_false(keyring):
    cur = FakeCursor()
    targets.create_in(cur, alias="alpha-svc", host="db.example.internal",
                      port=5432, default_database="ledger")
    sql, params = cur.calls[0]
    assert "INSERT INTO target_servers" in sql
    assert "enabled" in sql and "FALSE" in sql
    assert True not in params


def test_every_password_is_encrypted_on_the_way_in(keyring):
    cur = FakeCursor()
    targets.create_in(
        cur, alias="alpha-svc", host="db.example.internal", port=5432,
        default_database="ledger",
        credentials={"ro": ("reader", "ro-plaintext"),
                     "rw": ("writer", "rw-plaintext"),
                     "ddl": ("owner", "ddl-plaintext")})
    _, params = cur.calls[0]
    for secret in ("ro-plaintext", "rw-plaintext", "ddl-plaintext"):
        assert secret not in params, f"{secret} reached the INSERT in the clear"
        assert any(isinstance(p, str) and _decrypts_to(p, secret) for p in params), \
            f"{secret} was not stored as ciphertext at all"


def _decrypts_to(candidate: str, plaintext: str) -> bool:
    try:
        return crypto.decrypt(candidate) == plaintext
    except Exception:
        return False


def test_a_target_created_without_credentials_gets_the_sentinel(keyring):
    """The RO columns are NOT NULL, so "no credentials yet" needs a
    representation — and it has to be the one the executor and the schema
    snapshot already recognise, not a second spelling of the same idea."""
    cur = FakeCursor()
    targets.create_in(cur, alias="alpha-svc", host="db.example.internal",
                      port=5432, default_database="ledger")
    _, params = cur.calls[0]
    assert targets.DEFAULT_RO_USERNAME in params
    assert any(isinstance(p, str) and _decrypts_to(p, targets.SENTINEL_PASSWORD)
               for p in params)


def test_the_create_response_carries_no_password(wire):
    out = ra.admin_create_connection(
        ra.ConnectionIn(alias="alpha-svc", host="db.example.internal",
                        defaultDatabase="ledger",
                        credentials={"ro": {"username": "reader",
                                            "password": "s3cret"}}),
        claims=SUPER)
    assert "s3cret" not in repr(out)
    assert set(out["credentials"]["ro"]) == {"username", "configured",
                                             "placeholder"}


def test_the_audit_row_for_a_create_names_tiers_not_credentials(wire):
    ra.admin_create_connection(
        ra.ConnectionIn(alias="alpha-svc", host="db.example.internal",
                        defaultDatabase="ledger",
                        credentials={"ro": {"username": "reader",
                                            "password": "s3cret"}}),
        claims=SUPER)
    action, details = wire["audit"][-1]
    assert action == "connection_created"
    assert details["credentials"] == ["ro"]
    assert details["enabled"] is False
    assert "s3cret" not in repr(details) and "reader" not in repr(details)


def test_the_port_defaults_to_the_engine_default(wire, monkeypatch):
    seen = {}
    monkeypatch.setattr(ra.targets, "create_in",
                        lambda cur, **kw: seen.update(kw) or 77)
    ra.admin_create_connection(
        ra.ConnectionIn(alias="alpha-svc", host="db.example.internal",
                        defaultDatabase="ledger", engine="mssql"), claims=SUPER)
    assert seen["port"] == 1433


def test_a_duplicate_alias_is_a_conflict(wire):
    with pytest.raises(HTTPException) as e:
        ra.admin_create_connection(
            ra.ConnectionIn(alias="prod-beta", host="db.example.internal",
                            defaultDatabase="ledger"), claims=SUPER)
    assert e.value.status_code == 409


@pytest.mark.parametrize("field,value", [
    ("alias", "not an alias"),
    ("alias", "-leading-dash"),
    ("alias", ""),
    # The reason host validation exists: the SQL Server path interpolates the
    # host into an ODBC connection string, where `;` starts a new keyword.
    ("host", "db.example.internal;Trusted_Connection=yes"),
    ("host", "db.example.internal:5432"),
    ("host", "has space"),
    ("defaultDatabase", "two words"),
    ("defaultDatabase", ""),
])
def test_malformed_identifiers_are_refused(wire, field, value):
    body = {"alias": "alpha-svc", "host": "db.example.internal",
            "defaultDatabase": "ledger", field: value}
    with pytest.raises(HTTPException) as e:
        ra.admin_create_connection(ra.ConnectionIn(**body), claims=SUPER)
    assert e.value.status_code == 400


def test_only_engines_the_bot_can_run_may_be_registered(wire):
    """'clickhouse' passes the database CHECK constraint — it has a safety
    profile — but has no execution path, so a connection registered with it
    would fail closed at submit time with nothing on this screen to explain
    why."""
    with pytest.raises(HTTPException) as e:
        ra.admin_create_connection(
            ra.ConnectionIn(alias="alpha-svc", host="db.example.internal",
                            defaultDatabase="ledger", engine="clickhouse"),
            claims=SUPER)
    assert e.value.status_code == 400


# ---------------------------------------------------------------------------
# PATCH — enable guard, credential rotation, honest audit
# ---------------------------------------------------------------------------

def test_a_target_with_placeholder_credentials_cannot_be_enabled(wire):
    wire["row"] = _row(enabled=False)
    wire["row"]["credentials"]["ro"] = {"username": "queryhub_ro",
                                        "configured": True, "placeholder": True}
    with pytest.raises(HTTPException) as e:
        ra.admin_update_connection("prod-beta", ra.ConnectionPatch(enabled=True),
                                   claims=SUPER)
    assert e.value.status_code == 409
    assert "read-only credentials" in e.value.detail["message"]


def test_enabling_works_when_the_same_request_supplies_the_password(
        wire, monkeypatch):
    """Otherwise the only way to enable a freshly-imported target would be two
    round trips, and the form would have to guess which order works."""
    wire["row"] = _row(enabled=False)
    wire["row"]["credentials"]["ro"] = {"username": "queryhub_ro",
                                        "configured": True, "placeholder": True}
    written = []
    monkeypatch.setattr(ra.targets, "update_in",
                        lambda cur, tid, changes: written.append(changes) or list(changes))
    monkeypatch.setattr(ra.targets, "set_credentials_in",
                        lambda cur, tid, mode, username, password: None)
    ra.admin_update_connection(
        "prod-beta",
        ra.ConnectionPatch(enabled=True,
                           credentials={"ro": {"password": "real-one"}}),
        claims=SUPER)
    assert written == [{"enabled": True}]


def test_the_audit_entry_lists_only_fields_that_really_changed(wire, monkeypatch):
    """The form posts the whole record on every save. Without a comparison
    against the stored row, every edit would claim host, port and database
    changed — and the one edit that mattered would be impossible to find."""
    monkeypatch.setattr(ra.targets, "update_in",
                        lambda cur, tid, changes: list(changes))
    ra.admin_update_connection(
        "prod-beta",
        ra.ConnectionPatch(alias="prod-beta", host="db.example.internal",
                           port=5432, defaultDatabase="ledger",
                           notes="owned by the payments pod"),
        claims=SUPER)
    action, details = wire["audit"][-1]
    assert action == "connection_updated"
    assert details["changed"] == ["notes"]


def test_a_blank_password_box_does_not_wipe_a_stored_credential(wire, monkeypatch):
    """The edit form renders empty password boxes because the server never
    sends passwords back. Treating empty as "set it to nothing" would destroy a
    working credential the moment somebody fixed a typo in the host."""
    rotated = []
    monkeypatch.setattr(ra.targets, "set_credentials_in",
                        lambda cur, tid, mode, username, password:
                        rotated.append((mode, username, password)))
    monkeypatch.setattr(ra.targets, "update_in", lambda cur, tid, changes: [])
    ra.admin_update_connection(
        "prod-beta",
        ra.ConnectionPatch(credentials={"ro": {"username": "queryhub_ro",
                                               "password": ""},
                                        "rw": {"username": "", "password": ""}}),
        claims=SUPER)
    assert rotated == [("ro", "queryhub_ro", None)]


def test_set_credentials_encrypts_and_touches_only_the_named_tier(keyring):
    cur = FakeCursor()
    targets.set_credentials_in(cur, 42, "ddl", username="owner",
                               password="ddl-plaintext")
    sql, params = cur.calls[0]
    assert "username_ddl" in sql and "password_ddl_encrypted" in sql
    assert "password_encrypted =" not in sql and "password_rw_encrypted" not in sql
    assert "ddl-plaintext" not in params
    assert any(isinstance(p, str) and _decrypts_to(p, "ddl-plaintext")
               for p in params)


def test_set_credentials_rejects_an_unknown_tier():
    with pytest.raises(ValueError):
        targets.set_credentials_in(FakeCursor(), 42, "root", password="x")


def test_update_in_ignores_anything_outside_the_column_whitelist():
    """The column name is interpolated into the UPDATE, so the whitelist is the
    only thing standing between a request body and arbitrary SQL."""
    cur = FakeCursor()
    written = targets.update_in(cur, 42, {"host": "db.example.internal",
                                          "password_encrypted": "nope",
                                          "id = 1; DROP TABLE requests --": "x"})
    assert written == ["host"]
    sql, params = cur.calls[0]
    assert sql == ("UPDATE target_servers SET host = %s, updated_at = NOW() "
                   "WHERE id = %s")
    assert params == ("db.example.internal", 42)


# ---------------------------------------------------------------------------
# Connection test — an unreachable target is an answer, not an error
# ---------------------------------------------------------------------------

def test_a_failed_probe_is_reported_without_the_connection_detail(monkeypatch):
    """A libpq failure must not echo the DSN it was handed back to the browser.

    errors.scrub covers the shapes it knows — the `for user "..."` trailer,
    managed-cloud hostnames, private IPv4 — and a target on a private domain is
    none of them, so the probe redacts the host it dialled by value. (The
    private-IP branch of scrub is not asserted here: check_repo_clean.py
    refuses a literal RFC1918 address in a tracked file, and smuggling one past
    the scanner to test it would be worse than leaving it to scrub's own
    pattern.)
    """
    import psycopg

    def _boom(**kw):
        raise psycopg.OperationalError(
            'connection to server at "db.example.internal", port 5432 failed: '
            'FATAL: password authentication failed for user "queryhub_ro"')
    monkeypatch.setattr(psycopg, "connect", _boom)
    out = ra._probe("postgres", "db.example.internal", 5432, "ledger",
                    "queryhub_ro", "wrong")
    assert out["ok"] is False
    assert "db.example.internal" not in out["error"]
    assert "queryhub_ro" not in out["error"]
    # ...and it still says something useful, or the admin learns nothing.
    assert "authentication failed" in out["error"]


def test_a_probe_failure_still_drops_libpq_keyword_fragments(monkeypatch):
    """The other shape the same error takes: `host=... user=...`, which carries
    the endpoint and the role in one line."""
    import psycopg

    def _boom(**kw):
        raise psycopg.OperationalError(
            "could not connect: host=db.example.internal port=5432 "
            "user=queryhub_ro dbname=ledger")
    monkeypatch.setattr(psycopg, "connect", _boom)
    out = ra._probe("postgres", "db.example.internal", 5432, "ledger",
                    "queryhub_ro", "wrong")
    assert "db.example.internal" not in out["error"]
    assert "queryhub_ro" not in out["error"]


def test_the_probe_timeout_stays_short_enough_to_answer_a_request():
    """The probe runs synchronously inside the request and an unreachable host
    fails by timeout, so this constant IS the worst-case response time."""
    assert 0 < ra._PROBE_TIMEOUT_SEC <= 8


def test_testing_an_unprovisioned_target_never_dials_out(wire, monkeypatch):
    monkeypatch.setattr(ra.targets, "get_credentials",
                        lambda tid, mode: ("queryhub_ro",
                                           targets.SENTINEL_PASSWORD))
    monkeypatch.setattr(ra, "_probe", lambda *a, **k: pytest.fail(
        "probed a target whose password is the not-provisioned sentinel"))
    out = ra.admin_test_connection("prod-beta", claims=SUPER)
    assert out["ok"] is False
    assert "No read-only credentials" in out["error"]


def test_the_stored_probe_resolves_credentials_through_the_secrets_provider(
        wire, monkeypatch):
    """Reading the columns directly would make this button pass or fail for
    reasons unrelated to whether the executor could actually connect — targets
    can source their credentials from an external store."""
    seen = {}

    def _creds(tid, mode):
        seen["target_id"], seen["mode"] = tid, mode
        return ("reader", "from-the-vault")
    monkeypatch.setattr(ra.targets, "get_credentials", _creds)
    monkeypatch.setattr(ra, "_probe",
                        lambda *a, **k: {"ok": True, "args": a})
    out = ra.admin_test_connection("prod-beta", claims=SUPER)
    assert seen["mode"] == "ro"
    assert out["args"] == ("postgres", "db.example.internal", 5432, "ledger",
                           "reader", "from-the-vault")


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def test_the_list_includes_disabled_targets(wire):
    """A new connection starts disabled, so a list that hid disabled rows would
    hide the row the admin just created."""
    wire["row"] = _row(enabled=False)
    entry = ra.admin_connections(claims=SUPER)["connections"][0]
    assert entry["enabled"] is False


def test_a_listed_connection_exposes_no_ciphertext(wire):
    entry = ra.admin_connections(claims=SUPER)["connections"][0]
    assert "password" not in repr(entry).lower()
    # The raw engine id travels alongside the display label so the edit form
    # can round-trip a value the CHECK constraint accepts.
    assert entry["engineId"] == "postgres" and entry["engine"] == "PostgreSQL"
