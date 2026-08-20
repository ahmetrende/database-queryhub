"""Admin-panel endpoints (ADMIN_API.md): approval queue, decisions,
batch approve, and the kill switch.

Every route runs behind verify_session (deps.current_user) then
require_admin. Decisions reuse core_decide — the SAME pipeline the Slack
approval buttons run — so a web decision performs the identical DB
transition + audit row + Slack mirror + executor dispatch. The web panel
is an alternative surface, never a parallel or bypass path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import (
    access_requests,
    admins,
    audit,
    auto_approve,
    core_decide,
    core_submit,
    db,
    engines,
    errors,
    grants,
    schema_catalog,
    targets,
    teams,
)
from .. import config as cfg
from . import admin, config_admin, deps, mapping, metrics

_SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9]{8,}$")
# Local accounts are first-class principals: migration 075 widened the identity
# CHECK on ten tables to accept `local:<username>` precisely so a deployment
# without Slack could be administered. These endpoints kept validating against
# the Slack id shape alone, so in the vanilla profile the admin panel could not
# grant access to, scope, or whitelist any of the accounts it had just created —
# the profile was unusable exactly where it is the only option.
_LOCAL_ID_RE = re.compile(r"^local:[A-Za-z0-9._-]{1,64}$")


def _valid_principal(pid: str | None) -> bool:
    """True for either identity namespace. The two are disjoint by
    construction (a Slack id can't contain ':'), so this stays unambiguous."""
    pid = pid or ""
    return bool(_SLACK_ID_RE.match(pid) or _LOCAL_ID_RE.match(pid))

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(deps.block_pw_gate)])

# Columns the queue + scope check need (can_approve reads query /
# target_server_id / requester_slack_id).
_QUEUE_COLS = (
    "id, requester_slack_id, requester_name, target_server_id, database_name, "
    "query, justification, status, scheduled_for, bundle_id, position, "
    "risk_summary, origin, created_at, engine, required_tier, "
    # Items of one `/sql batch` submission are one piece of work to the person
    # who sent them and to the person approving them, but they arrive as N
    # separate rows. `position` and the sibling count let the queue present
    # them as the batch they are instead of N unrelated queries interleaved
    # with everyone else's.
    "(SELECT count(*) FROM requests sib "
    "  WHERE sib.bundle_id = requests.bundle_id) AS bundle_size"
)
# Minimal slice for a scope check on a single request. Carries engine +
# required_tier so can_approve reads the engine-aware tier persisted at
# submit instead of re-deriving it (SEC-ENG).
_SCOPE_COLS = ("id, query, target_server_id, requester_slack_id, status, "
               "engine, required_tier")


def _alias_of(target_id) -> str | None:
    if target_id is None:
        return None
    t = targets.get(int(target_id))
    return t.alias if t else None


def _tags_of(target_id) -> dict:
    """The target's hosting bag, resolved here rather than in the client.

    The queue is the one place the join has to be server-side: the approver is
    being told where a statement will run, and a client that joined it against
    its own connection list would be showing a cloud name that may be minutes
    or months out of date next to a DROP.
    """
    if target_id is None:
        return {}
    t = targets.get(int(target_id))
    return (getattr(t, "tags", None) or {}) if t else {}


def _slack_client():
    """Bot-token Slack client used to mirror a web decision back into
    Slack (same token deps' employment check uses). None in the vanilla
    profile (no Slack) — core_decide.apply_effects then no-ops every Slack
    side effect, so a web approval works with no Slack SDK installed."""
    if not cfg.ENV.slack_enabled:
        return None
    from slack_sdk import WebClient
    return WebClient(token=cfg.ENV.slack_bot_token)


# ---- Review: approval queue -------------------------------------------------

@router.get("/queue")
def admin_queue(escalate: bool | None = None,
                claims: dict = Depends(deps.current_user)):
    """Pending queries this admin may approve (scoped by tier + connection
    via the same can_approve the Slack side uses). `?escalate=true` filters
    to DDL escalations, `?escalate=false` to the rest."""
    uid = admin.require_admin(claims, "review")
    rows = db.fetch_all(
        f"SELECT {_QUEUE_COLS} FROM requests WHERE status = 'pending' "
        f"ORDER BY id")
    out = []
    for r in rows:
        if not admins.can_approve(uid, r):
            continue
        item = mapping.queue_item(r, _alias_of, _tags_of)
        if escalate is not None and item["escalate"] != escalate:
            continue
        out.append(item)
    return {"queue": out}


# ---- Review: decision -------------------------------------------------------

class DecisionIn(BaseModel):
    decision: str
    note: str | None = None


@router.post("/queue/{request_id}/decision")
def admin_decision(request_id: int, body: DecisionIn,
                   claims: dict = Depends(deps.current_user)):
    """Approve / reject / request changes on one request. Mirrors into
    Slack and (on immediate approve) dispatches to the executor."""
    # Authorization first, before input validation and before any lookup. The
    # scope check needs the row, so it cannot all happen up here — but plain
    # admin-ness can, and must: reaching the `requests` SELECT as a non-admin
    # turns 404-vs-other into an id-enumeration oracle, and reaching the
    # decision check leaks the accepted values.
    admin.require_admin(claims, "review")
    decision = (body.decision or "").strip().lower()
    if decision not in ("approve", "reject", "changes"):
        raise deps._error(400, "bad_request",
                          "decision must be approve, reject, or changes.")
    row = db.fetch_one(
        f"SELECT {_SCOPE_COLS} FROM requests WHERE id = %s", (request_id,))
    if row is None:
        raise deps._error(404, "not_found", "No such request.")
    # Scope gate: tier + connection, enforced server-side regardless of UI.
    uid = admin.require_admin(claims, "review", request=row)
    reason = (body.note or "").strip()
    if decision in ("reject", "changes") and not reason:
        raise deps._error(400, "bad_request",
                          "A note is required to reject or request changes.")
    outcome = core_decide.decide(
        request_id, decision, by_id=uid, by_name=claims.get("name"),
        reason=reason or None)
    if outcome is None:
        raise deps._error(409, "conflict",
                          "This request was already decided.")
    core_decide.apply_effects(_slack_client(), outcome)
    return {"id": str(request_id),
            "status": mapping.status_to_web(outcome.row["status"])}


# ---- Review: batch approve --------------------------------------------------

class BatchIn(BaseModel):
    ids: list[str] = Field(default_factory=list)


@router.post("/queue/batch-approve")
def admin_batch_approve(body: BatchIn,
                        claims: dict = Depends(deps.current_user)):
    """Approve several requests at once. Silently skips anything the caller
    can't approve, doesn't exist, or was already decided — returns the count
    actually approved."""
    uid = admin.require_admin(claims, "review")
    client = _slack_client()
    approved = 0
    for raw in body.ids:
        try:
            rid = int(raw)
        except (TypeError, ValueError):
            continue
        row = db.fetch_one(
            f"SELECT {_SCOPE_COLS} FROM requests WHERE id = %s", (rid,))
        if row is None or not admins.can_approve(uid, row):
            continue
        outcome = core_decide.decide(rid, "approve", by_id=uid,
                                     by_name=claims.get("name"))
        if outcome is not None:
            core_decide.apply_effects(client, outcome)
            approved += 1
    return {"approved": approved}


# ---- Kill switch ------------------------------------------------------------

class KillIn(BaseModel):
    enabled: bool
    message: str | None = None


@router.get("/kill")
def get_kill(claims: dict = Depends(deps.current_user)):
    admin.require_admin(claims, "review")
    on = core_submit.kill_switch_on()
    by = at = None
    if on:
        # who engaged it + when — from the latest 'kill_switch_set' audit row
        # that turned it ON (the UI banner shows "Paused by X · Ym ago").
        row = db.fetch_one(
            "SELECT actor_name, actor_slack_id, created_at FROM audit_log "
            "WHERE action = 'kill_switch_set' AND details->>'enabled' = 'true' "
            "ORDER BY id DESC LIMIT 1")
        if row:
            by = row["actor_name"] or row["actor_slack_id"]
            at = row["created_at"].isoformat() if row["created_at"] else None
    return {"enabled": on, "message": core_submit.kill_switch_message(),
            "by": by, "at": at}


@router.post("/kill")
def set_kill(body: KillIn, claims: dict = Depends(deps.current_user)):
    """Halt / resume all new query traffic. Super-admin only — this stops
    the whole fleet. Writes bot_config (runtime-effective) + an audit row."""
    uid = admin.require_admin(claims, "access")
    val = "on" if body.enabled else "off"
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO bot_config (key, value, updated_at) "
            "VALUES ('kill_switch', %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "  updated_at = NOW()",
            (val,))
        msg = (body.message or "").strip()
        if msg:
            cur.execute(
                "INSERT INTO bot_config (key, value, updated_at) "
                "VALUES ('kill_switch_message', %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "  updated_at = NOW()",
                (msg,))
        audit.log_in(cur, None, uid, claims.get("name"), "kill_switch_set",
                     {"enabled": body.enabled})
    return {"enabled": body.enabled,
            "message": core_submit.kill_switch_message()}


# ---- System configuration (super-admin only) --------------------------------

class ConfigIn(BaseModel):
    changes: dict = Field(default_factory=dict)


@router.get("/config")
def admin_config(claims: dict = Depends(deps.current_user)):
    """Every fleet-wide setting from bot_config, typed + grouped for the
    System configuration screen. Real values, no mock."""
    admin.require_admin(claims, "access")
    return {"config": config_admin.build_config()}


@router.put("/config")
def admin_save_config(body: ConfigIn, claims: dict = Depends(deps.current_user)):
    """Write changed bot_config keys (runtime-effective) + one audit row.
    Only existing keys are editable; values are type-coerced server-side."""
    uid = admin.require_admin(claims, "access")
    with db.transaction() as cur:
        try:
            applied = config_admin.apply_config(body.changes, cur)
        except ValueError as e:
            # A cross-key invariant would break — e.g. the orphan-reconciler
            # lease dropping to or below the query timeout, which would make the
            # reconciler fail queries that are still running. Refuse the write.
            raise deps._error(400, "bad_request", str(e)) from e
        if applied:
            # Drop the read cache so the operator's own change is visible on the
            # next request instead of up to a TTL later.
            cfg.invalidate_cache()
            audit.log_in(cur, None, uid, claims.get("name"), "config_set",
                         {"changes": [{"key": a["key"], "to": str(a["to"])[:200]}
                                      for a in applied]})
    return {"config": config_admin.build_config(), "applied": len(applied)}


# ---- Access control (super-admin only): read views --------------------------
# All gated need="access". Mutations (POST/DELETE/PUT) land in a follow-up
# slice so they can reuse the existing grant/scope cores + the auth-event DM
# path, never re-implement access logic here.

@router.get("/grants")
def admin_grants(claims: dict = Depends(deps.current_user)):
    """Every active per-user + per-team grant across the fleet. Grants are
    (subject, target) with an allowed-databases list; `id` is synthesized so
    a later DELETE can address the composite key."""
    admin.require_admin(claims, "access")
    out = []
    for row in db.fetch_all(
            "SELECT g.slack_user_id AS subject, r.name AS subject_name, "
            "  g.target_server_id, g.allowed_databases, g.mode, "
            "  g.granted_by, g.granted_at, g.expires_at "
            "FROM user_target_grants g "
            "LEFT JOIN requesters r ON r.slack_user_id = g.slack_user_id "
            "WHERE g.revoked_at IS NULL ORDER BY g.granted_at DESC"):
        row["_subject_type"] = "user"
        row["_gid"] = f"u:{row['subject']}:{row['target_server_id']}"
        out.append(mapping.grant_entry(row, _alias_of))
    for row in db.fetch_all(
            "SELECT g.team_id, t.name AS subject_name, g.target_server_id, "
            "  g.allowed_databases, g.mode, g.granted_at, g.expires_at "
            "FROM team_target_grants g LEFT JOIN teams t ON t.id = g.team_id "
            "WHERE g.revoked_at IS NULL ORDER BY g.granted_at DESC"):
        row["subject"] = str(row["team_id"])
        row["_subject_type"] = "team"
        row["_gid"] = f"t:{row['team_id']}:{row['target_server_id']}"
        row["granted_by"] = None
        out.append(mapping.grant_entry(row, _alias_of))
    return {"grants": out}


@router.get("/auto-grants")
def admin_auto_grants(claims: dict = Depends(deps.current_user)):
    """Active (unexpired) auto-approve grants — the rows that let a
    developer's query skip DBA review."""
    admin.require_admin(claims, "access")
    # Names, not just ids. The table's subject column showed a raw Slack id,
    # which reads as an opaque token to the person deciding whether a grant
    # should still exist. A principal is a requester or an admin (or both), so
    # both tables are consulted and the first name found wins.
    rows = db.fetch_all(
        "SELECT g.id, g.slack_user_id, g.max_tier, g.target_server_id, "
        "       g.database_name, g.reason, g.expires_at, g.granted_by, "
        "       g.granted_at, "
        "       COALESCE(r.name,  a.name)  AS user_name, "
        "       COALESCE(gr.name, ga.name) AS granted_by_name "
        "  FROM auto_approve_grants g "
        "  LEFT JOIN requesters r  ON r.slack_user_id  = g.slack_user_id "
        "  LEFT JOIN admins     a  ON a.slack_user_id  = g.slack_user_id "
        "  LEFT JOIN requesters gr ON gr.slack_user_id = g.granted_by "
        "  LEFT JOIN admins     ga ON ga.slack_user_id = g.granted_by "
        " WHERE g.expires_at IS NULL OR g.expires_at > NOW() "
        " ORDER BY g.granted_at DESC")
    return {"autoGrants": [mapping.auto_grant_entry(r, _alias_of) for r in rows]}


@router.get("/scopes")
def admin_scopes(claims: dict = Depends(deps.current_user)):
    """Every current admin (permanent + active temp) with their derived
    role / approvable tiers / connection scope — reuses admin_block so the
    scope shown is exactly the one the gate enforces."""
    admin.require_admin(claims, "access")
    out = []
    for a in admins.list_active():
        blk = admin.admin_block(a["slack_user_id"]) or {}
        out.append({
            "id": a["slack_user_id"],
            "admin": a.get("name") or a["slack_user_id"],
            "role": blk.get("role", "dba"),
            "canApprove": blk.get("canApprove", []),
            "connections": blk.get("connections", []),
            "source": a.get("source"),
            "expiresAt": mapping.iso(a.get("expires_at")),
        })
    return {"scopes": out}


# ---- Connections: the target-server registry (super-admin) ------------------
# Reads were the whole story here until the registry became editable from the
# web. Everything below is deliberately narrow about credentials: a password
# goes IN through these endpoints and never comes back out, so no response
# shape — not the list, not the create echo, not an error — can carry one.

# Aliases end up in URL paths, Slack pickers and admin scope lists, so keep them
# to a set that survives all three unquoted.
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
# Hosts are validated because they are NOT always passed as a parameter: the
# SQL Server path builds an ODBC connection string where UID/PWD/DATABASE are
# brace-quoted but SERVER is interpolated, so a host containing `;` could append
# ODBC keywords of its own choosing. A hostname or IP literal has no business
# containing one.
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
# Database names and role names: anything without whitespace or the quoting
# characters that let a value escape its field.
_IDENT_RE = re.compile(r"^[^\s;'\"\\]{1,128}$")

# A probe has to be short: it runs synchronously inside the request, and an
# unreachable host in a closed subnet fails by TIMEOUT rather than by refusal,
# so this number IS the response time in the failure case. Five seconds covers a
# cross-region TLS handshake and still returns before the admin decides the page
# has hung.
_PROBE_TIMEOUT_SEC = 5


def _connection_entry(row: dict, databases: list[str]) -> dict:
    """One registry row as the admin UI reads it."""
    return {
        "id": row["alias"], "name": row["alias"],
        "engine": mapping.engine_label(row["engine"]),
        # The raw engine id as well as the display label: the edit form has to
        # round-trip the value the CHECK constraint accepts, and "PostgreSQL"
        # is not it.
        "engineId": row["engine"],
        "env": mapping.env_of(row["alias"]),
        "enabled": row["enabled"],
        "host": row["host"],
        "port": row["port"],
        "defaultDatabase": row["default_database"],
        "notes": row["notes"],
        "secretsProvider": row["secrets_provider"],
        "tags": row.get("tags") or {},
        "credentials": row["credentials"],
        "databases": [{"id": d, "name": d} for d in databases],
    }


# The three keys the UI gives real controls to. Everything else is a free
# key/value pair a DBA-admin invents on the connection form. Reserved keys are
# always offered, at zero count, so the vocabulary does not depend on somebody
# having used them first.
TAG_RESERVED = ("provider", "service", "account")
_TAG_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TAG_MAX_KEYS = 24
TAG_MAX_VALUE = 120


def _clean_tags(raw: dict | None) -> dict:
    """Validate and normalise a whole tag bag.

    Keys are lower-case and shaped like identifiers because they become search
    tokens (`provider:aws`) and a filter dimension across the fleet; a key with
    a space or a colon in it could not be typed back. Values are trimmed
    strings, and an empty value drops the key rather than storing a blank —
    "the key is present but says nothing" is the state that makes a tag lie.

    Bounded on both axes so one connection form cannot write a payload every
    /connections call then has to carry.
    """
    if raw is None:
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k or "").strip().lower()
        if not _TAG_KEY_RE.match(key):
            raise deps._error(
                422, "validation",
                f"Tag key '{k}' is not usable — lower-case letters, digits, "
                f"'-' and '_' only, starting with a letter (max 32).")
        val = ("" if v is None else str(v)).strip()
        if not val:
            continue
        if len(val) > TAG_MAX_VALUE:
            raise deps._error(422, "validation",
                              f"Tag '{key}' is too long (max {TAG_MAX_VALUE}).")
        out[key] = val
    if len(out) > TAG_MAX_KEYS:
        raise deps._error(422, "validation",
                          f"Too many tags (max {TAG_MAX_KEYS}).")
    return out


def _connection_payload(row: dict) -> dict:
    from . import routes_data
    return _connection_entry(row, routes_data._catalog_databases(row["id"]))


def _require_target_row(conn: str) -> dict:
    row = targets.by_alias(conn)
    if row is None:
        raise deps._error(404, "not_found", f"Unknown connection '{conn}'.")
    detail = targets.admin_row(row.id)
    if detail is None:                     # deleted between the two reads
        raise deps._error(404, "not_found", f"Unknown connection '{conn}'.")
    return detail


def _clean_engine(engine: str | None) -> str:
    """Validate an engine id against the engines the bot can actually run.

    Narrower than the database CHECK constraint on purpose. The column also
    accepts 'clickhouse' so a target can be TAGGED with an engine whose safety
    profile exists before its execution path does — but letting the admin panel
    register one would create a connection that fails closed at submit time for
    reasons the operator has no way to see from this screen.
    """
    e = (engine or "postgres").strip().lower()
    if e not in engines.WIRED_ENGINES:
        allowed = ", ".join(sorted(engines.WIRED_ENGINES))
        raise deps._error(400, "bad_request", f"engine must be one of: {allowed}.")
    return e


def _clean_alias(alias: str | None) -> str:
    a = (alias or "").strip()
    if not _ALIAS_RE.match(a):
        raise deps._error(400, "bad_request",
                          "alias must be 1-63 characters: letters, digits, "
                          "dot, dash or underscore, starting alphanumeric.")
    return a


def _clean_host(host: str | None) -> str:
    h = (host or "").strip()
    if not _HOST_RE.match(h):
        raise deps._error(400, "bad_request",
                          "host must be a hostname or IP address.")
    return h


def _clean_port(port: int | None, engine: str) -> int:
    if port is None:
        return engines.spec(engine).default_port
    try:
        p = int(port)
    except (TypeError, ValueError):
        p = 0
    if not 1 <= p <= 65535:
        raise deps._error(400, "bad_request", "port must be between 1 and 65535.")
    return p


def _clean_ident(value: str | None, what: str) -> str:
    v = (value or "").strip()
    if not _IDENT_RE.match(v):
        raise deps._error(400, "bad_request",
                          f"{what} must not be empty or contain whitespace or "
                          f"quote characters.")
    return v


def _clean_credentials(raw: dict | None) -> dict[str, tuple[str | None, str | None]]:
    """Validate the {tier: {username, password}} block into {tier: (u, p)}.

    An empty string is treated as "not supplied" rather than "set it to
    nothing": a form that renders a blank password box on every edit would
    otherwise wipe a working credential the moment someone fixes a typo in the
    host field.
    """
    out: dict[str, tuple[str | None, str | None]] = {}
    for tier, cred in (raw or {}).items():
        mode = (tier or "").strip().lower()
        if mode not in ("ro", "rw", "ddl"):
            raise deps._error(400, "bad_request",
                              "credential tiers are ro, rw and ddl.")
        username = (cred.username or "").strip() if cred.username else ""
        password = cred.password or ""
        if username:
            _clean_ident(username, "username")
        if not username and not password:
            continue
        out[mode] = (username or None, password or None)
    return out


def _probe(engine: str, host: str, port: int, database: str,
           username: str, password: str) -> dict:
    """Open one connection with the given credential, read the server version,
    close. Returns {ok, latencyMs, serverVersion, error}.

    A failed probe is a successful call: "cannot reach it" is the answer the
    admin asked for, not an error in the API. The message is run through the
    same scrubber the executor uses before a libpq or ODBC error reaches a
    user, so a failure can't echo back the DSN it was handed.
    """
    import time
    started = time.monotonic()
    version = None
    try:
        if engine == "mssql":
            from .. import mssql_exec
            conn = mssql_exec.connect(host, port, database, username, password,
                                      timeout_sec=_PROBE_TIMEOUT_SEC)
            try:
                cur = conn.cursor()
                cur.execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(64))")
                row = cur.fetchone()
                version = row[0] if row else None
            finally:
                conn.close()
        else:
            import psycopg
            with psycopg.connect(
                    host=host, port=port, dbname=database, user=username,
                    password=password, connect_timeout=_PROBE_TIMEOUT_SEC,
                    **cfg.target_ssl_kwargs(),
                    application_name="queryhub-connection-test",
                    options="-c statement_timeout=5000 "
                            "-c default_transaction_read_only=on") as conn:
                with conn.cursor() as cur:
                    cur.execute("SHOW server_version")
                    row = cur.fetchone()
                    version = row[0] if row else None
    except ImportError as e:
        # The SQL Server driver is optional and only installed on hosts that
        # serve MSSQL targets. Say that plainly — the scrubbed pyodbc message
        # ("No module named ...") reads like a bug in QueryHub.
        log.warning("connection test: driver unavailable for %s: %r", engine, e)
        return {"ok": False, "latencyMs": None, "serverVersion": None,
                "error": "No driver for this engine is installed on the "
                         "QueryHub host."}
    except Exception as e:
        log.info("connection test failed for %s:%s (%s): %r",
                 host, port, engine, e)
        message = errors.scrub(e)
        # scrub() redacts the hostname SHAPES it knows — managed-cloud
        # endpoints, private IPv4 ranges, libpq `host=` fragments — and a
        # target on a private domain matches none of them, so libpq's
        # `connection to server at "..."` preamble survives it. This probe is
        # the one place that knows exactly which host it dialled, so redact it
        # by value rather than hoping a pattern covers the next naming scheme.
        if host:
            message = message.replace(host, "the host")
        return {"ok": False,
                "latencyMs": int((time.monotonic() - started) * 1000),
                "serverVersion": None, "error": message}
    return {"ok": True, "latencyMs": int((time.monotonic() - started) * 1000),
            "serverVersion": str(version) if version else None, "error": None}


class CredentialIn(BaseModel):
    username: str | None = None
    password: str | None = None       # write-only; never returned by any route


class ConnectionIn(BaseModel):
    alias: str
    host: str
    defaultDatabase: str
    port: int | None = None           # None = the engine's default port
    engine: str = "postgres"
    notes: str | None = None
    tags: dict[str, str] | None = None
    credentials: dict[str, CredentialIn] = Field(default_factory=dict)


class ConnectionPatch(BaseModel):
    alias: str | None = None
    host: str | None = None
    defaultDatabase: str | None = None
    port: int | None = None
    engine: str | None = None
    notes: str | None = None
    enabled: bool | None = None
    # The WHOLE bag, replacing what is stored — never a merge. A merge patch
    # cannot express "this key is gone", and a tag that survives its own
    # deletion is worse than no tag: it keeps answering for a machine that has
    # moved. `None` means "not editing tags"; `{}` means "remove them all".
    tags: dict[str, str] | None = None
    credentials: dict[str, CredentialIn] = Field(default_factory=dict)


class ConnectionTestIn(BaseModel):
    host: str
    defaultDatabase: str
    username: str
    password: str
    port: int | None = None
    engine: str = "postgres"


@router.get("/connections")
def admin_connections(claims: dict = Depends(deps.current_user)):
    """Every registered target, unfiltered by grant (the developer
    /connections is grant-scoped; this is the admin's full view). No per-user
    `tier` — it is meaningless without a grant to derive it from.

    Disabled targets are included, which they were not before this screen
    could create one: a new connection starts disabled, so a list that showed
    only enabled rows would hide the row the admin had just added.
    """
    admin.require_admin(claims, "access")
    return {"connections": [_connection_payload(r)
                            for r in targets.list_admin_rows()]}


@router.get("/tag-keys")
def admin_tag_keys(claims: dict = Depends(deps.current_user)):
    """The tag vocabulary, DERIVED from the fleet rather than stored beside it.

    A key exists because a connection carries it, so a key nobody uses cannot
    linger in the picker and a key somebody invented is offered to the next
    person without anyone maintaining a list. The form uses this for
    suggestions and to warn when a new key is about to become a fleet-wide
    filter dimension.

    Reserved keys are always present, at zero count, so the three that have
    real controls do not appear and disappear depending on whether anyone has
    filled them in yet.
    """
    admin.require_admin(claims, "access")
    counts: dict[str, dict] = {
        k: {"key": k, "label": k.capitalize(), "reserved": True,
            "count": 0, "values": {}}
        for k in TAG_RESERVED
    }
    for row in db.fetch_all(
            "SELECT COALESCE(tags, '{}'::jsonb) AS tags FROM target_servers"):
        for k, v in (row["tags"] or {}).items():
            e = counts.setdefault(k, {"key": k, "label": k.capitalize(),
                                      "reserved": False, "count": 0,
                                      "values": {}})
            e["count"] += 1
            e["values"][str(v)] = e["values"].get(str(v), 0) + 1
    keys = []
    for e in counts.values():
        keys.append({
            "key": e["key"], "label": e["label"], "reserved": e["reserved"],
            "count": e["count"],
            # Commonest first: the picker's job is to make the value somebody
            # already used the easy one to pick again, which is what keeps a
            # free-text field from turning into six spellings of "production".
            "values": [{"value": val, "count": n} for val, n in
                       sorted(e["values"].items(), key=lambda kv: (-kv[1], kv[0]))],
        })
    # Reserved keys first and in DECLARED order — provider, then service, then
    # account — because that is how specific they are and how the form asks for
    # them. Alphabetical would open with `account`, which is the one a DBA fills
    # in last. Invented keys follow, alphabetically, having no natural order.
    order = {k: i for i, k in enumerate(TAG_RESERVED)}
    keys.sort(key=lambda e: (0, order[e["key"]], "") if e["reserved"]
              else (1, 0, e["key"]))
    return {"keys": keys}


@router.post("/connections", status_code=201)
def admin_create_connection(body: ConnectionIn,
                            claims: dict = Depends(deps.current_user)):
    """Register a new target server. Always created DISABLED — see
    targets.create_in()."""
    uid = admin.require_admin(claims, "access")
    engine = _clean_engine(body.engine)
    alias = _clean_alias(body.alias)
    host = _clean_host(body.host)
    port = _clean_port(body.port, engine)
    database = _clean_ident(body.defaultDatabase, "defaultDatabase")
    creds = _clean_credentials(body.credentials)
    if targets.by_alias(alias) is not None:
        raise deps._error(409, "conflict",
                          f"A connection named '{alias}' already exists.")
    with db.transaction() as cur:
        new_id = targets.create_in(
            cur, alias=alias, host=host, port=port, default_database=database,
            engine=engine, notes=(body.notes or "").strip() or None,
            tags=_clean_tags(body.tags), credentials=creds)
        # Deliberately no usernames in the details blob, let alone passwords:
        # keeping it to "which tiers were filled" means no reader of the audit
        # log ever has to judge whether a field in here was a secret.
        audit.log_in(cur, None, uid, claims.get("name"), "connection_created",
                     {"connection": alias, "target_id": new_id, "host": host,
                      "port": port, "engine": engine, "enabled": False,
                      "credentials": sorted(creds)})
    return _connection_payload(targets.admin_row(new_id))


@router.patch("/connections/{conn}")
def admin_update_connection(conn: str, body: ConnectionPatch,
                            claims: dict = Depends(deps.current_user)):
    """Edit one connection: any field, the enabled flag, and any of the three
    credentials. Absent fields are left alone — this is a patch, not a
    replace, because the client is never given the passwords it would need to
    send back on a full replace."""
    uid = admin.require_admin(claims, "access")
    row = _require_target_row(conn)
    target_id = row["id"]
    engine = _clean_engine(body.engine) if body.engine is not None else row["engine"]
    creds = _clean_credentials(body.credentials)

    # Every field is compared against the stored row before it counts as a
    # change. The form posts the whole record on every save, so without this
    # the audit entry would claim host, port and database were edited each
    # time somebody fixed a typo in the notes — which makes the one entry that
    # matters impossible to spot.
    changes: dict = {}

    def _set(column: str, value):
        if value != row[column]:
            changes[column] = value

    if body.alias is not None:
        alias = _clean_alias(body.alias)
        if alias != row["alias"]:
            if targets.by_alias(alias) is not None:
                raise deps._error(409, "conflict",
                                  f"A connection named '{alias}' already exists.")
            changes["alias"] = alias
    if body.host is not None:
        _set("host", _clean_host(body.host))
    if body.port is not None:
        _set("port", _clean_port(body.port, engine))
    if body.defaultDatabase is not None:
        _set("default_database",
             _clean_ident(body.defaultDatabase, "defaultDatabase"))
    if body.engine is not None:
        _set("engine", engine)
    if body.notes is not None:
        _set("notes", (body.notes or "").strip() or None)
    if body.tags is not None:
        # Whole-bag replace. `_set` compares against the stored value, so
        # re-saving the form with the same tags is still not a change and still
        # writes no audit row.
        _set("tags", _clean_tags(body.tags))
    if body.enabled is not None and bool(body.enabled) != row["enabled"]:
        # Enabling is the moment a target becomes reachable by developers, so
        # it is the moment to insist the credential is real. Without this an
        # admin can enable a freshly-imported placeholder, watch it appear in
        # every picker, and only find out it was never provisioned when
        # somebody's query fails on a sentinel password.
        ro = row["credentials"]["ro"]
        rotating_ro = "ro" in creds and creds["ro"][1]
        if body.enabled and not rotating_ro and (not ro["configured"]
                                                 or ro["placeholder"]):
            raise deps._error(
                409, "conflict",
                f"'{row['alias']}' has no read-only credentials yet — set them "
                f"before enabling it.")
        changes["enabled"] = bool(body.enabled)

    # A save that changed nothing writes nothing — no empty transaction and,
    # more to the point, no audit row. An audit trail padded with "updated"
    # entries that record no change is one nobody reads.
    if not changes and not creds:
        return _connection_payload(row)

    with db.transaction() as cur:
        written = targets.update_in(cur, target_id, changes)
        for mode, (username, password) in creds.items():
            targets.set_credentials_in(cur, target_id, mode, username, password)
        details: dict = {"connection": row["alias"], "target_id": target_id,
                         "changed": written, "credentials": sorted(creds)}
        # The new value is worth recording for the two fields whose change is
        # the security-relevant event; the rest are named but not quoted, so
        # the entry stays free of anything that could be a secret.
        if "enabled" in changes:
            details["enabled"] = changes["enabled"]
        if "alias" in changes:
            details["renamed_to"] = changes["alias"]
        if "tags" in changes:
            # Name the tag change, not just the fact that "tags" moved. These
            # are the words an operator will search the log for six months from
            # now — "when did prod-main stop saying AWS" — and they are labels,
            # never credentials, so quoting them costs nothing. Both sides,
            # because a tag that was REMOVED is the interesting half.
            details["tags_before"] = row.get("tags") or {}
            details["tags_after"] = changes["tags"]
            details["hosting"] = " · ".join(
                str(changes["tags"][k]) for k in TAG_RESERVED
                if changes["tags"].get(k)) or None
        audit.log_in(cur, None, uid, claims.get("name"), "connection_updated",
                     details)
    return _connection_payload(targets.admin_row(target_id))


@router.delete("/connections/{conn}")
def admin_delete_connection(conn: str,
                            claims: dict = Depends(deps.current_user)):
    """Remove a connection — or, if anything still references it, disable it
    and say so.

    Not a 409: the operator's intent ("stop using this target") is carried out
    either way, and reporting the fallback as a failure would leave them
    thinking nothing happened when in fact the connection is now dark. The
    response says which of the two occurred and why.
    """
    uid = admin.require_admin(claims, "access")
    row = _require_target_row(conn)
    target_id, alias = row["id"], row["alias"]
    refs = targets.reference_counts(target_id)
    blocking = {k: v for k, v in refs.items()
                if v and k in ("requests", "csv_imports", "user_grants",
                               "team_grants", "auto_grants")}
    if blocking:
        reason = ("This connection has " + ", ".join(
            f"{v} {k.replace('_', ' ')}" for k, v in sorted(blocking.items()))
            + " — deleting it would rewrite history or silently drop access, "
              "so it was disabled instead.")
        with db.transaction() as cur:
            targets.update_in(cur, target_id, {"enabled": False})
            audit.log_in(cur, None, uid, claims.get("name"),
                         "connection_updated",
                         {"connection": alias, "target_id": target_id,
                          "changed": ["enabled"], "enabled": False,
                          "reason": "delete refused: still referenced",
                          "references": blocking})
        return {"connection": alias, "deleted": False, "disabled": True,
                "reason": reason, "references": refs}
    with db.transaction() as cur:
        targets.delete_in(cur, target_id)
        audit.log_in(cur, None, uid, claims.get("name"), "connection_deleted",
                     {"connection": alias, "target_id": target_id,
                      "host": row["host"], "engine": row["engine"]})
    return {"connection": alias, "deleted": True, "disabled": False,
            "reason": None, "references": refs}


@router.post("/connections/test")
def admin_test_new_connection(body: ConnectionTestIn,
                              claims: dict = Depends(deps.current_user)):
    """Try credentials that are not saved yet — the Add-connection form's
    "Test" button, so a target is proven reachable before it is registered."""
    admin.require_admin(claims, "access")
    engine = _clean_engine(body.engine)
    host = _clean_host(body.host)
    port = _clean_port(body.port, engine)
    database = _clean_ident(body.defaultDatabase, "defaultDatabase")
    username = _clean_ident(body.username, "username")
    return _probe(engine, host, port, database, username, body.password or "")


@router.post("/connections/{conn}/test")
def admin_test_connection(conn: str,
                          claims: dict = Depends(deps.current_user)):
    """Try the STORED read-only credential for one registered connection.

    Resolved through targets.get_credentials() rather than read from the
    columns, so a target whose secrets live in an external store is tested the
    same way the executor would use it — otherwise this button would pass or
    fail for reasons unrelated to whether a query could actually run.
    """
    admin.require_admin(claims, "access")
    row = _require_target_row(conn)
    try:
        username, password = targets.get_credentials(row["id"], "ro")
    except Exception as e:
        log.info("connection test: no RO credentials for %s: %r", conn, e)
        username = password = None
    if not username or not password or password == targets.SENTINEL_PASSWORD:
        # An unconfigured target is an ok:false answer, not an HTTP error: the
        # caller asked "can this connect?" and "it has no credentials" is the
        # answer, rendered in the same place as a refused login would be.
        return {"ok": False, "latencyMs": None, "serverVersion": None,
                "error": "No read-only credentials are stored for this "
                         "connection yet."}
    return _probe(row["engine"], row["host"], row["port"],
                  row["default_database"], username, password)


@router.post("/connections/{conn}/schema-refresh")
def admin_schema_refresh(conn: str, database: str | None = None,
                         claims: dict = Depends(deps.current_user)):
    """Re-snapshot a connection's schema on demand (admin-panel button).

    The hourly cron refreshes the whole fleet; this lets an admin pull a
    single target's tables/columns right after a DDL change instead of
    waiting up to an hour. Reuses the exact snapshot path the cron uses (RO,
    read-only, 8s connect + 60s statement timeout), one target only — bounded
    work, no background job needed.

    `database` narrows it to one. A connection can carry a dozen databases and
    re-reading all of them to pick up one changed table is most of a minute of
    waiting for a result the caller did not ask for — which is the difference
    between a right-click on a database being useful and being avoided."""
    uid = admin.require_admin(claims, "review")
    t = targets.by_alias(conn)
    if t is None:
        raise deps._error(404, "not_found", f"Unknown connection '{conn}'.")
    try:
        password = targets.get_password(t.id)
    except LookupError:
        password = None
    if not password or password == targets.SENTINEL_PASSWORD:
        raise deps._error(409, "conflict",
                          f"Connection '{conn}' has no stored credentials to read its schema.")
    try:
        databases = schema_catalog.list_target_databases(t, password)
    except Exception as e:
        log.warning("schema refresh: cannot reach %s: %r", conn, e)
        raise deps._error(502, "upstream",
                          f"Could not reach '{conn}' to refresh its schema.")
    if database is not None:
        # Checked against what the server actually serves, not against the
        # catalog: refreshing is precisely what you do when the catalog is
        # stale, so a database missing from it is not evidence of anything.
        if database not in databases:
            raise deps._error(
                404, "not_found",
                f"'{conn}' has no database named '{database}'.")
        databases = [database]
    results: dict[str, dict] = {}
    total_tables = 0
    for dbname in databases:
        try:
            n_tables, n_cols = schema_catalog.snapshot_database(t, password, dbname)
            results[dbname] = {"tables": n_tables, "columns": n_cols}
            total_tables += n_tables
        except Exception:
            log.exception("schema refresh failed for %s/%s", conn, dbname)
            results[dbname] = {"error": True}
    with db.transaction() as cur:
        audit.log_in(cur, None, uid, claims.get("name"), "schema_refreshed",
                     {"connection": conn, "databases": results,
                      "scope": database or "all"})
    return {"connection": conn, "databases": results, "tables": total_tables,
            "scope": database or "all"}


@router.get("/endpoint-requests")
def admin_endpoint_requests(status: str | None = None,
                            claims: dict = Depends(deps.current_user)):
    """Access / endpoint provisioning requests (access_requests). Optional
    ?status filter (e.g. pending)."""
    admin.require_admin(claims, "access")
    cols = ("id, requester_slack_id, requester_name, target_server_id, "
            "database_name, reason, status, created_at")
    if status:
        rows = db.fetch_all(
            f"SELECT {cols} FROM access_requests WHERE status = %s "
            f"ORDER BY id DESC LIMIT 200", (status,))
    else:
        rows = db.fetch_all(
            f"SELECT {cols} FROM access_requests ORDER BY id DESC LIMIT 200")
    return {"requests": [mapping.endpoint_request_entry(r, _alias_of) for r in rows]}


# ---- Access control (super-admin only): mutations ---------------------------
# User-grant + auto-grant writes only, this slice. User grants reuse the
# proven grants.grant/revoke cores (whitelist + upsert + audit + grantee DM,
# auth-event-aware). Auto-grants write directly + let the auth-event trigger
# DM the user (no app.auth_dm_suppress). Team grants and admin-scope edits
# stay on Slack for now (no clean web core yet).

def _target_id_of(alias: str | None) -> int | None:
    if not alias:
        return None
    row = db.fetch_one("SELECT id FROM target_servers WHERE alias = %s", (alias,))
    return row["id"] if row else None


def _resolve_team(name: str | None) -> dict | None:
    """Existing team by (case-insensitive) name. Teams are created deliberately
    (SQL / Slack), never from the web — this only looks one up."""
    if not (name or "").strip():
        return None
    return db.fetch_one(
        "SELECT id, name FROM teams WHERE lower(name) = lower(%s)", (name.strip(),))


def _count_super_admins() -> int:
    """Active permanent admins with all three scope columns NULL (= super).
    The last one must never be removed or demoted, or nobody can grant."""
    return db.fetch_one(
        "SELECT count(*) AS n FROM admins WHERE enabled = TRUE "
        "AND max_tier IS NULL AND scope_team_ids IS NULL "
        "AND scope_target_ids IS NULL")["n"]


def _parse_grant_id(gid: str) -> dict | None:
    """`u:<slackId>:<targetId>` / `t:<teamId>:<targetId>` → parts, else None."""
    parts = (gid or "").split(":")
    if len(parts) != 3 or parts[0] not in ("u", "t"):
        return None
    try:
        return {"kind": parts[0], "subject": parts[1], "target_id": int(parts[2])}
    except ValueError:
        return None


def _slack_profile(uid: str) -> dict:
    """Best-effort name/email/tz for a freshly-granted user (so a new
    whitelist row is complete). Never raises."""
    if not cfg.ENV.slack_enabled:
        return {}  # vanilla profile: no Slack to look a profile up in
    try:
        from slack_sdk import WebClient
        u = WebClient(token=cfg.ENV.slack_bot_token).users_info(user=uid)["user"]
        prof = u.get("profile", {}) or {}
        return {"name": u.get("real_name") or prof.get("display_name"),
                "email": prof.get("email"), "tz": u.get("tz")}
    except Exception:
        return {}


class GrantIn(BaseModel):
    subjectType: str = "user"
    subject: str
    connectionId: str
    databaseId: str | None = None
    databases: list[str] | None = None
    tier: str = "ro"
    reason: str | None = None
    # ISO-8601, or null for "no expiry" — which is what every grant issued
    # before migration 096 has, and what most will keep. A grant given for one
    # afternoon's migration should be able to say so; nothing forces it to.
    expiresAt: str | None = None


@router.post("/grants", status_code=201)
def admin_create_grant(body: GrantIn, claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    stype = (body.subjectType or "user").lower()
    if stype not in ("user", "team"):
        raise deps._error(400, "bad_request", "subjectType must be user or team.")
    tier = (body.tier or "ro").lower()
    if tier not in ("ro", "rw", "ddl"):
        raise deps._error(400, "bad_request", "tier must be RO, RW, or DDL.")
    tid = _target_id_of(body.connectionId)
    if tid is None:
        raise deps._error(404, "not_found", "Unknown connection.")
    # The bot's own control-plane database is never grantable. The Slack modal
    # enforced this; this endpoint did not, so the same operation was refused in
    # one UI and allowed in the other — and the team branch below writes
    # team_target_grants directly, bypassing grants.grant() where the check now
    # lives. Guard both branches here, at the entrance.
    if tid in grants.control_plane_target_ids():
        raise deps._error(
            403, "forbidden",
            "That connection is the bot's own control-plane database — "
            "granting access to it would allow tampering with the audit log "
            "and the admin list.")
    dbs = body.databases or ([body.databaseId] if body.databaseId else None)
    # Expiry (migration 096). Parsed here, before either branch writes, so a
    # malformed date is a 400 rather than half a grant. A date already in the
    # past is refused rather than accepted-and-inert: writing a grant that is
    # dead on arrival reads to the admin as "access given", and the row would
    # sit in the list looking live.
    expires_at = None
    if body.expiresAt:
        try:
            expires_at = datetime.fromisoformat(body.expiresAt.replace("Z", "+00:00"))
        except ValueError:
            raise deps._error(400, "bad_request",
                              "expiresAt must be an ISO-8601 timestamp.")
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise deps._error(400, "bad_request",
                              "expiresAt is in the past — that grant would "
                              "never apply.")

    if stype == "team":
        team = _resolve_team(body.subject)
        if team is None:
            raise deps._error(404, "not_found",
                              f"No team named '{body.subject}'. Create the team "
                              "in Slack / SQL first — teams aren't created here.")
        # Upsert the team→target grant; the auth_event trigger on
        # team_target_grants DMs every affected team member automatically.
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO team_target_grants "
                "  (team_id, target_server_id, allowed_databases, mode, expires_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (team_id, target_server_id) DO UPDATE "
                "  SET allowed_databases = EXCLUDED.allowed_databases, "
                "      mode = EXCLUDED.mode, "
                "      expires_at = EXCLUDED.expires_at, "
                "      revoked_at = NULL",
                (team["id"], tid, dbs, tier, expires_at))
            audit.log_in(cur, None, uid, claims.get("name"), "team_grant_added",
                         {"team": team["name"], "team_id": team["id"],
                          "target_id": tid, "databases": dbs, "tier": tier,
                          "expires_at": expires_at.isoformat() if expires_at else None})
        return {"id": f"t:{team['id']}:{tid}", "subjectType": "team",
                "subject": team["name"], "connectionId": body.connectionId,
                "databases": dbs or "*", "tier": tier.upper()}

    if not _valid_principal(body.subject):
        raise deps._error(400, "bad_request", "subject must be a principal id: a Slack user id or local:<username>.")
    summary = grants.grant(
        granter_id=uid, granter_name=claims.get("name"), grantee_id=body.subject,
        grantee_profile=_slack_profile(body.subject), target_id=tid, mode=tier,
        databases=dbs, reason=body.reason, notify=True, expires_at=expires_at)
    return {"id": f"u:{body.subject}:{tid}", "subjectType": "user",
            "subject": body.subject, "connectionId": body.connectionId,
            "databases": summary["databases"] or "*",
            "tier": summary["mode"].upper()}


@router.delete("/grants/{gid}", status_code=204)
def admin_delete_grant(gid: str, claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    parsed = _parse_grant_id(gid)
    if parsed is None:
        raise deps._error(400, "bad_request", "Bad grant id.")
    if parsed["kind"] == "t":
        try:
            team_id = int(parsed["subject"])
        except ValueError:
            raise deps._error(400, "bad_request", "Bad team grant id.")
        with db.transaction() as cur:
            cur.execute(
                "DELETE FROM team_target_grants "
                "WHERE team_id = %s AND target_server_id = %s RETURNING team_id",
                (team_id, parsed["target_id"]))
            if cur.fetchone() is None:
                raise deps._error(404, "not_found", "No team grant to revoke.")
            audit.log_in(cur, None, uid, claims.get("name"), "team_grant_removed",
                         {"team_id": team_id, "target_id": parsed["target_id"]})
        return
    row = grants.revoke(granter_id=uid, granter_name=claims.get("name"),
                        grantee_id=parsed["subject"], target_id=parsed["target_id"],
                        notify=True)
    if row is None:
        raise deps._error(404, "not_found", "No active grant to revoke.")


# ---------- People directory + Teams (super-admin) ----------
def _initials(name: str | None, fallback: str) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return (fallback or "?")[:2].upper()
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


@router.get("/people")
def admin_people(claims: dict = Depends(deps.current_user)):
    """Developer directory (enabled requesters) — the pool the Teams screen
    assigns from and the Grants subject picker lists. `handle` == slack_user_id
    so it lines up 1:1 with grant subjects."""
    admin.require_admin(claims, "access")
    rows = db.fetch_all(
        "SELECT slack_user_id, name FROM requesters WHERE enabled = TRUE "
        "ORDER BY lower(coalesce(name, slack_user_id))")
    return {"people": [{
        "id": r["slack_user_id"], "handle": r["slack_user_id"],
        "slackId": r["slack_user_id"], "name": r["name"] or r["slack_user_id"],
        "initials": _initials(r["name"], r["slack_user_id"]),
    } for r in rows]}


def _teams_payload() -> list[dict]:
    teams = db.fetch_all(
        "SELECT id, name, description FROM teams ORDER BY lower(name)")
    members = db.fetch_all("SELECT team_id, slack_user_id FROM team_members")
    by_team: dict[int, list[str]] = {}
    for m in members:
        by_team.setdefault(m["team_id"], []).append(m["slack_user_id"])
    # subteams is always [] — the grant model has no team nesting (see teams.py
    # effective_grant_for_user), so the web UI does not offer it.
    return [{
        "id": str(t["id"]), "name": t["name"], "desc": t["description"] or "",
        "members": by_team.get(t["id"], []), "subteams": [],
    } for t in teams]


@router.get("/teams")
def admin_teams(claims: dict = Depends(deps.current_user)):
    """Every team with its members (super-admin view)."""
    admin.require_admin(claims, "access")
    return {"teams": _teams_payload()}


def _valid_member_ids(ids) -> list[str]:
    """Keep only well-formed Slack ids that are enabled requesters — a member
    who is not a whitelisted user could never use the team's grants anyway,
    and this stops a typo'd id from being written into membership."""
    want = [m for m in (ids or []) if _valid_principal(m)]
    if not want:
        return []
    rows = db.fetch_all(
        "SELECT slack_user_id FROM requesters "
        "WHERE enabled = TRUE AND slack_user_id = ANY(%s)", (want,))
    keep = {r["slack_user_id"] for r in rows}
    return [m for m in want if m in keep]


class TeamIn(BaseModel):
    name: str
    desc: str | None = None
    members: list[str] | None = None


@router.post("/teams", status_code=201)
def admin_create_team(body: TeamIn, claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    name = (body.name or "").strip()
    if not name:
        raise deps._error(400, "bad_request", "Team name is required.")
    if _resolve_team(name):
        raise deps._error(409, "conflict", f"A team named '{name}' already exists.")
    members = _valid_member_ids(body.members)
    with db.transaction() as cur:
        cur.execute("INSERT INTO teams (name, description) VALUES (%s, %s) "
                    "RETURNING id", (name, (body.desc or "").strip() or None))
        tid = cur.fetchone()["id"]
        for m in members:
            cur.execute("INSERT INTO team_members (team_id, slack_user_id) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING", (tid, m))
        audit.log_in(cur, None, uid, claims.get("name"), "team_created",
                     {"team": name, "team_id": tid, "members": members})
    return {"id": str(tid), "name": name, "desc": (body.desc or "").strip(),
            "members": members, "subteams": []}


@router.put("/teams/{team_id}")
def admin_update_team(team_id: int, body: TeamIn,
                      claims: dict = Depends(deps.current_user)):
    """Rename/re-describe a team and reconcile its membership in one save. The
    team_members trigger DMs every added/removed member automatically."""
    uid = admin.require_admin(claims, "access")
    team = db.fetch_one("SELECT id, name FROM teams WHERE id = %s", (team_id,))
    if team is None:
        raise deps._error(404, "not_found", "No such team.")
    name = (body.name or "").strip()
    if not name:
        raise deps._error(400, "bad_request", "Team name is required.")
    if name.lower() != team["name"].lower() and _resolve_team(name):
        raise deps._error(409, "conflict", f"A team named '{name}' already exists.")
    desired = set(_valid_member_ids(body.members))
    with db.transaction() as cur:
        cur.execute("UPDATE teams SET name = %s, description = %s WHERE id = %s",
                    (name, (body.desc or "").strip() or None, team_id))
        cur.execute("SELECT slack_user_id FROM team_members WHERE team_id = %s",
                    (team_id,))
        existing = {r["slack_user_id"] for r in cur.fetchall()}
        to_add, to_remove = desired - existing, existing - desired
        if to_remove:
            cur.execute("DELETE FROM team_members WHERE team_id = %s "
                        "AND slack_user_id = ANY(%s)", (team_id, list(to_remove)))
        for m in to_add:
            cur.execute("INSERT INTO team_members (team_id, slack_user_id) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING", (team_id, m))
        audit.log_in(cur, None, uid, claims.get("name"), "team_updated",
                     {"team": name, "team_id": team_id,
                      "added": sorted(to_add), "removed": sorted(to_remove)})
    return {"id": str(team_id), "name": name, "desc": (body.desc or "").strip(),
            "members": sorted(desired), "subteams": []}


@router.delete("/teams/{team_id}", status_code=204)
def admin_delete_team(team_id: int, claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    team = db.fetch_one("SELECT id, name FROM teams WHERE id = %s", (team_id,))
    if team is None:
        raise deps._error(404, "not_found", "No such team.")
    with db.transaction() as cur:
        # team_members + team_target_grants cascade on delete; their auth_event
        # triggers DM every affected member (lost membership + lost access).
        cur.execute("DELETE FROM teams WHERE id = %s", (team_id,))
        audit.log_in(cur, None, uid, claims.get("name"), "team_deleted",
                     {"team": team["name"], "team_id": team_id})
    return


class PersonTeamsIn(BaseModel):
    teams: list[str] = []


@router.put("/people/{slack_id}/teams")
def admin_set_person_teams(slack_id: str, body: PersonTeamsIn,
                           claims: dict = Depends(deps.current_user)):
    """Set the FULL team membership of one person (Teams → People tab)."""
    uid = admin.require_admin(claims, "access")
    if not _valid_principal(slack_id):
        raise deps._error(400, "bad_request", "Bad principal id (expected a Slack user id or local:<username>).")
    desired: set[int] = set()
    for t in body.teams or []:
        try:
            desired.add(int(t))
        except (TypeError, ValueError):
            pass
    if desired:  # keep only teams that actually exist
        rows = db.fetch_all("SELECT id FROM teams WHERE id = ANY(%s)",
                            (list(desired),))
        desired = {r["id"] for r in rows}
    with db.transaction() as cur:
        cur.execute("SELECT team_id FROM team_members WHERE slack_user_id = %s",
                    (slack_id,))
        existing = {r["team_id"] for r in cur.fetchall()}
        to_add, to_remove = desired - existing, existing - desired
        if to_remove:
            cur.execute("DELETE FROM team_members WHERE slack_user_id = %s "
                        "AND team_id = ANY(%s)", (slack_id, list(to_remove)))
        for tid in to_add:
            cur.execute("INSERT INTO team_members (team_id, slack_user_id) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING", (tid, slack_id))
        audit.log_in(cur, None, uid, claims.get("name"), "person_teams_set",
                     {"slack_id": slack_id, "teams": sorted(desired),
                      "added": sorted(to_add), "removed": sorted(to_remove)})
    return {"slackId": slack_id, "teams": [str(t) for t in sorted(desired)]}


class CopyAccessIn(BaseModel):
    """Give one person the access another already has."""
    source: str                       # principal to copy FROM
    includeTeams: bool = True         # copy team membership as well as grants
    tier: str | None = None           # override every copied tier, e.g. "rw"


@router.post("/people/{slack_id}/copy-access", status_code=201)
def admin_copy_access(slack_id: str, body: CopyAccessIn,
                      claims: dict = Depends(deps.current_user)):
    """Copy a colleague's access onto this person.

    Onboarding is nearly always "give them what X has", and doing it by hand
    means reading X's grants, remembering that some of them arrive through a
    team, and typing the list back. That is where targets get missed.

    Two shapes, and the difference matters:

    * `includeTeams` (default) — join the same teams and copy the source's own
      per-user grants. Access keeps tracking the team, including grants the
      team gains later.
    * `includeTeams: false` — write EXPLICIT per-user grants for everything the
      source can reach today, team-derived targets included. Without expanding
      those, dropping team membership silently drops most of the access, since
      that is where it usually comes from. This is the shape to use while teams
      are being replaced by pods: no new membership rows to migrate, and the
      newcomer does not inherit whatever the team is granted later.

    `tier` overrides every copied grant, because "the same servers, but
    read-only" is a routine ask and copying RW by accident is not recoverable
    by the person who notices.
    """
    uid = admin.require_admin(claims, "access")
    for pid in (slack_id, body.source):
        if not _valid_principal(pid):
            raise deps._error(400, "bad_request",
                              f"Bad principal id: {pid!r}.")
    if slack_id == body.source:
        raise deps._error(400, "bad_request", "Source and target are the same person.")
    tier = (body.tier or "").lower() or None
    if tier is not None and tier not in ("ro", "rw", "ddl"):
        raise deps._error(400, "bad_request", "tier must be RO, RW, or DDL.")

    # Never copy a grant on the bot's own control-plane database, whatever the
    # source happens to hold: that is the one target whose access would allow
    # editing the audit trail this endpoint writes to.
    forbidden = set(grants.control_plane_target_ids())

    with db.transaction() as cur:
        cur.execute(
            "SELECT target_server_id, allowed_databases, mode "
            "  FROM user_target_grants "
            " WHERE slack_user_id = %s AND revoked_at IS NULL "
            "   AND (expires_at IS NULL OR expires_at > NOW())",
            (body.source,))
        src_grants = {r["target_server_id"]: r for r in cur.fetchall()}

        cur.execute(
            "SELECT g.target_server_id, g.allowed_databases, g.mode "
            "  FROM team_target_grants g "
            "  JOIN team_members m ON m.team_id = g.team_id "
            " WHERE m.slack_user_id = %s AND g.revoked_at IS NULL "
            "   AND (g.expires_at IS NULL OR g.expires_at > NOW())",
            (body.source,))
        team_grants = list(cur.fetchall())

        teams_joined: list[int] = []
        if body.includeTeams:
            cur.execute(
                "INSERT INTO team_members (team_id, slack_user_id) "
                "SELECT team_id, %s FROM team_members WHERE slack_user_id = %s "
                "ON CONFLICT DO NOTHING RETURNING team_id",
                (slack_id, body.source))
            teams_joined = [r["team_id"] for r in cur.fetchall()]
            to_write = src_grants
        else:
            # Team-derived targets become explicit grants. A user row supersedes
            # the team's for that target, so where both exist the source's own
            # grant wins — it is the narrower, deliberate one.
            merged = {r["target_server_id"]: r for r in team_grants}
            merged.update(src_grants)
            to_write = merged

        written: list[int] = []
        for tid, g in sorted(to_write.items()):
            if tid in forbidden:
                continue
            cur.execute(
                "INSERT INTO user_target_grants "
                "  (slack_user_id, target_server_id, allowed_databases, mode, granted_by) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (slack_user_id, target_server_id) DO NOTHING",
                (slack_id, tid, g["allowed_databases"], tier or g["mode"], uid))
            if cur.rowcount:
                written.append(tid)

        audit.log_in(cur, None, uid, claims.get("name"), "access_copied",
                     {"to": slack_id, "from": body.source,
                      "targets_granted": written,
                      "teams_joined": teams_joined,
                      "include_teams": body.includeTeams,
                      "tier_override": tier,
                      "skipped_control_plane": sorted(
                          set(to_write) & forbidden)})

    return {"slackId": slack_id, "copiedFrom": body.source,
            "targetsGranted": len(written), "teamsJoined": len(teams_joined),
            "tier": tier}


@router.get("/people/{slack_id}/effective-access")
def admin_effective_access(slack_id: str,
                           claims: dict = Depends(deps.current_user)):
    """What this person can actually reach, resolved the way a submission
    resolves it.

    "Why can they not see that server?" is answered today by reading three
    tables and applying the precedence rules by hand — user grant beats team
    grant, an expired grant is not a grant, an admin bypasses the whole
    question. Getting that wrong in either direction is expensive: a real
    problem dismissed, or access handed out that was already there.

    So this asks `teams.effective_grant_for_user`, the same resolver the
    executor uses, rather than re-deriving the answer. It reports; it changes
    nothing and impersonates nobody, and the audit row names the admin who
    looked.
    """
    uid = admin.require_admin(claims, "access")
    if not _valid_principal(slack_id):
        raise deps._error(400, "bad_request",
                          "Bad principal id (expected a Slack user id or local:<username>).")

    person = db.fetch_one(
        "SELECT slack_user_id, name, email, enabled, 'requester' AS kind "
        "  FROM requesters WHERE slack_user_id = %s "
        "UNION ALL "
        "SELECT slack_user_id, name, email, enabled, 'admin' "
        "  FROM admins WHERE slack_user_id = %s",
        (slack_id, slack_id))

    teams_of = db.fetch_all(
        "SELECT t.id, t.name FROM team_members m JOIN teams t ON t.id = m.team_id "
        " WHERE m.slack_user_id = %s ORDER BY t.name", (slack_id,))

    out = []
    for t in targets.list_all():
        g = teams.effective_grant_for_user(slack_id, t.id)
        if g is None:
            continue
        out.append({
            "connectionId": t.alias,
            "enabled": t.enabled,
            "tier": (g.get("mode") or "ro").upper(),
            # NULL means every database on the target, which is not the same
            # as an empty list and must not render as "no databases".
            "databases": g.get("allowed_databases"),
            "allDatabases": g.get("allowed_databases") is None,
            "source": g.get("source"),
        })

    auto = db.fetch_all(
        "SELECT max_tier, target_server_id, database_name, expires_at "
        "  FROM auto_approve_grants "
        " WHERE slack_user_id = %s AND starts_at <= NOW() "
        "   AND (expires_at IS NULL OR expires_at > NOW())", (slack_id,))

    with db.transaction() as cur:
        audit.log_in(cur, None, uid, claims.get("name"), "effective_access_viewed",
                     {"subject": slack_id, "targets": len(out)})

    return {
        "slackId": slack_id,
        "name": (person or {}).get("name"),
        "kind": (person or {}).get("kind"),
        "enabled": (person or {}).get("enabled"),
        "known": person is not None,
        "teams": [{"id": str(r["id"]), "name": r["name"]} for r in teams_of],
        "access": out,
        "autoApprove": [{
            "connectionId": _alias_of(r["target_server_id"]),
            "tier": (r["max_tier"] or "ro").upper(),
            "databaseId": r["database_name"],
            "allDatabases": r["database_name"] is None,
            "expiresAt": mapping.iso(r["expires_at"]),
        } for r in auto],
    }


class AutoGrantIn(BaseModel):
    user: str
    connectionId: str
    databaseId: str | None = None
    tier: str = "ro"
    reason: str | None = None
    expiresAt: str | None = None            # ISO-8601; None + no minutes = permanent
    expiresInMinutes: int | None = None


@router.post("/auto-grants", status_code=201)
def admin_create_auto_grant(body: AutoGrantIn,
                            claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    tier = (body.tier or "ro").lower()
    if tier not in ("ro", "rw", "ddl"):
        raise deps._error(400, "bad_request", "tier must be RO, RW, or DDL.")
    if not _valid_principal(body.user):
        raise deps._error(400, "bad_request", "user must be a principal id: a Slack user id or local:<username>.")
    tid = _target_id_of(body.connectionId)
    if tid is None:
        raise deps._error(404, "not_found", "Unknown connection.")
    # `*` (the form's own default for "every database") is not a wildcard the
    # matcher understands — it compares a non-NULL scope for equality, so the
    # literal star produced a grant that never fired and never complained.
    db_scope = auto_approve.normalise_scope(body.databaseId)
    try:
        auto_approve.validate_scope(tid, db_scope)
    except auto_approve.ScopeError as e:
        raise deps._error(400, "bad_request", str(e))
    # NOT suppressing app.auth_dm_suppress: the auth-event trigger DMs the user.
    with db.transaction() as cur:
        if body.expiresInMinutes:
            cur.execute(
                "INSERT INTO auto_approve_grants (slack_user_id, max_tier, "
                "  target_server_id, database_name, expires_at, reason, granted_by) "
                "VALUES (%s, %s, %s, %s, NOW() + make_interval(mins => %s), %s, %s) "
                "RETURNING id",
                (body.user, tier, tid, db_scope, body.expiresInMinutes,
                 body.reason, uid))
        else:
            cur.execute(
                "INSERT INTO auto_approve_grants (slack_user_id, max_tier, "
                "  target_server_id, database_name, expires_at, reason, granted_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (body.user, tier, tid, db_scope, body.expiresAt,
                 body.reason, uid))
        new_id = cur.fetchone()["id"]
        audit.log_in(cur, None, uid, claims.get("name"), "auto_approve_granted",
                     {"user": body.user, "target_id": tid,
                      "database": db_scope, "tier": tier})
    return {"id": str(new_id), "user": body.user, "tier": tier.upper(),
            "connectionId": body.connectionId, "databaseId": body.databaseId}


@router.delete("/auto-grants/{grant_id}", status_code=204)
def admin_delete_auto_grant(grant_id: int,
                            claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    with db.transaction() as cur:
        cur.execute(
            "DELETE FROM auto_approve_grants WHERE id = %s "
            "RETURNING slack_user_id, target_server_id", (grant_id,))
        row = cur.fetchone()
        if row is None:
            raise deps._error(404, "not_found", "No such auto-grant.")
        audit.log_in(cur, None, uid, claims.get("name"), "auto_approve_revoked",
                     {"grant_id": grant_id, "user": row["slack_user_id"],
                      "target_id": row["target_server_id"]})


# ---- Endpoint / access requests: decision (super-admin) ---------------------

class EndpointDecisionIn(BaseModel):
    approve: bool
    note: str | None = None


@router.post("/endpoint-requests/{req_id}/decision")
def admin_decide_endpoint(req_id: int, body: EndpointDecisionIn,
                          claims: dict = Depends(deps.current_user)):
    """Decide a developer's access request. Approve auto-grants inside
    access_requests.decide() itself — per-user, at the REQUESTED tier
    (default ro), same transaction as the status flip, shared with the
    Slack approve button. The auth-event outbox DMs the grantee. decide()
    skips the grant (flagged in auto_grant) when the target is unknown or
    an active grant exists at a different tier."""
    uid = admin.require_admin(claims, "access")
    req = access_requests.get(req_id)
    if req is None:
        raise deps._error(404, "not_found", "No such request.")
    if req["status"] != "pending":
        raise deps._error(409, "conflict", "This request was already decided.")

    status = "approved" if body.approve else "rejected"
    row = access_requests.decide(req_id, status, uid, claims.get("name"), body.note)
    if row is None:  # lost the race — another admin just decided it
        raise deps._error(409, "conflict", "This request was already decided.")

    if not body.approve:
        with db.transaction() as cur:
            audit.log_in(cur, None, uid, claims.get("name"), "endpoint_rejected",
                         {"request_id": req_id})
    return mapping.endpoint_request_entry(row, _alias_of)


# ---- Admin scopes: create / update / remove (super-admin) --------------------
# Permanent admins carry their scope inline: super = all three scope columns
# NULL (+ can_grant); a scoped DBA = max_tier (+ optional target scope). The
# auth_event trigger on `admins` DMs the affected user. The last super-admin can
# never be demoted or removed, or nobody can grant.

_TIER_ORDER = {"ro": 0, "rw": 1, "ddl": 2}


class ScopeIn(BaseModel):
    admin: str = ""                        # principal id (Slack or local:)
    role: str = "dba"                                   # dba | super
    canApprove: list[str] = Field(default_factory=list)  # RO/RW/DDL (dba only)
    connections: list[str] = Field(default_factory=list)  # aliases, or [] = all


def _apply_scope(body: ScopeIn, uid: str, actor_name: str | None) -> dict:
    target = body.admin
    if not _valid_principal(target):
        raise deps._error(400, "bad_request", "admin must be a principal id: a Slack user id or local:<username>.")
    role = (body.role or "dba").lower()
    if role not in ("dba", "super"):
        raise deps._error(400, "bad_request", "role must be dba or super.")
    if role != "super" and admins.is_super_admin(target) and _count_super_admins() <= 1:
        raise deps._error(409, "conflict",
                          "Can't demote the last super-admin — promote another first.")

    if role == "super":
        max_tier, target_ids, can_grant = None, None, True
    else:
        tiers = [t.lower() for t in (body.canApprove or []) if t.lower() in _TIER_ORDER]
        if not tiers:
            raise deps._error(400, "bad_request",
                              "A DBA needs at least one approvable tier (RO/RW/DDL).")
        max_tier = max(tiers, key=lambda t: _TIER_ORDER[t])
        conns = [c for c in (body.connections or []) if c and c != "*"]
        target_ids = None
        if conns:
            target_ids = []
            for c in conns:
                tid = _target_id_of(c)
                if tid is None:
                    raise deps._error(404, "not_found", f"Unknown connection '{c}'.")
                target_ids.append(tid)
        can_grant = False

    admin_name = _slack_profile(target).get("name")
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO admins (slack_user_id, name, added_by, enabled, "
            "  max_tier, scope_team_ids, scope_target_ids, can_grant) "
            "VALUES (%s, %s, %s, TRUE, %s, NULL, %s, %s) "
            "ON CONFLICT (slack_user_id) DO UPDATE SET "
            "  enabled = TRUE, name = COALESCE(EXCLUDED.name, admins.name), "
            "  max_tier = EXCLUDED.max_tier, scope_team_ids = EXCLUDED.scope_team_ids, "
            "  scope_target_ids = EXCLUDED.scope_target_ids, can_grant = EXCLUDED.can_grant",
            (target, admin_name, uid, max_tier, target_ids, can_grant))
        audit.log_in(cur, None, uid, actor_name, "admin_scope_set",
                     {"admin": target, "role": role, "max_tier": max_tier,
                      "scope_target_ids": target_ids})
    blk = admin.admin_block(target) or {}
    return {"id": target, "admin": admin_name or target,
            "role": blk.get("role", role), "canApprove": blk.get("canApprove", []),
            "connections": blk.get("connections", [])}


@router.post("/scopes", status_code=201)
def admin_create_scope(body: ScopeIn, claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    return _apply_scope(body, uid, claims.get("name"))


@router.put("/scopes/{admin_id}")
def admin_update_scope(admin_id: str, body: ScopeIn,
                       claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    body.admin = admin_id   # the path id wins over the body
    return _apply_scope(body, uid, claims.get("name"))


@router.delete("/scopes/{admin_id}", status_code=204)
def admin_delete_scope(admin_id: str, claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    if admins.is_super_admin(admin_id) and _count_super_admins() <= 1:
        raise deps._error(409, "conflict", "Can't remove the last super-admin.")
    with db.transaction() as cur:
        cur.execute("UPDATE admins SET enabled = FALSE "
                    "WHERE slack_user_id = %s AND enabled = TRUE "
                    "RETURNING slack_user_id", (admin_id,))
        if cur.fetchone() is None:
            raise deps._error(404, "not_found",
                              "No active permanent admin to remove "
                              "(temp admin grants are managed in Slack).")
        audit.log_in(cur, None, uid, claims.get("name"), "admin_scope_removed",
                     {"admin": admin_id})


# ---- Insights (review): audit / metrics / feedback --------------------------

# The registry-CRUD actions are not in mapping.AUDIT_KIND, so the SQL filter
# below would drop them and the screen that writes them could not show them
# back. Listed here explicitly; admin_audit_entry falls back to kind "other"
# and an underscore->space label, which already reads correctly ("Connection
# created"). Folding them into AUDIT_KIND belongs with the next pass over the
# admin surface,
# which owns the filter chips these kinds drive.
_CONNECTION_ACTIONS = ("connection_created", "connection_updated",
                       "connection_deleted")
_AUDIT_ACTIONS = tuple(mapping.AUDIT_KIND.keys()) + _CONNECTION_ACTIONS


@router.get("/audit")
def admin_audit(kind: str | None = None, q: str | None = None,
                limit: int = 100, claims: dict = Depends(deps.current_user)):
    """Immutable admin audit trail — a filtered view over audit_log
    (admin-relevant actions only), with optional ?kind and ?q free-text."""
    admin.require_admin(claims, "review")
    limit = max(1, min(int(limit), 500))
    # Free-text search runs in SQL over the WHOLE audit_log (actor, requester,
    # target alias/db, action and the full query), NOT over a client-loaded
    # window — otherwise anything older than the newest `limit` events is
    # invisible to search. Matches are then ordered newest-first and capped.
    params: list = [list(_AUDIT_ACTIONS)]
    where = "al.action = ANY(%s)"
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        where += (" AND (al.actor_name ILIKE %s OR r.requester_name ILIKE %s "
                  "OR ts.alias ILIKE %s OR r.database_name ILIKE %s "
                  "OR al.action ILIKE %s OR r.query ILIKE %s "
                  "OR al.details::text ILIKE %s OR r.origin ILIKE %s)")
        params += [like, like, like, like, like, like, like, like]
    params.append(limit)
    rows = db.fetch_all(
        "SELECT al.id, al.request_id, al.actor_slack_id, al.actor_name, al.action, "
        "  al.details, al.created_at, "
        "  r.requester_name AS req_requester_name, "
        "  r.target_server_id AS req_target_server_id, "
        "  r.database_name AS req_database_name, r.query AS req_query, "
        "  r.row_count AS req_row_count, r.executed_at AS req_executed_at, "
        "  r.completed_at AS req_completed_at, r.origin AS req_origin "
        # audit_log, NOT audit_log_reportable. The reportable views exist to keep
        # operator self-test traffic out of PRODUCT METRICS, and that exclusion
        # was inherited here by accident: `requests_reportable` drops every
        # request whose requester is in `report_excluded_users`, and
        # `audit_log_reportable` then drops every audit row attached to those
        # requests. The effect on this screen was that the operator's own
        # queries — the super-admin's, the most privileged activity there is —
        # were the one thing the audit trail would not show.
        #
        # An audit trail that omits a class of actors is not an audit trail.
        # Metrics keep using the reportable views; this reads everything.
        "FROM audit_log al "
        "LEFT JOIN requests r ON r.id = al.request_id "
        "LEFT JOIN target_servers ts ON ts.id = r.target_server_id "
        f"WHERE {where} "
        "ORDER BY al.id DESC LIMIT %s", tuple(params))
    # Rows without a linked request (auto-approve windows, grants, scopes) carry
    # the subject as a raw Slack ID in details / actor_slack_id. Resolve those to
    # display names via `requesters`, batched, so the table never shows a bare ID.
    ids = set()
    for r in rows:
        d = r.get("details") if isinstance(r.get("details"), dict) else {}
        for v in (d.get("user"), d.get("grantee"), d.get("slack_user_id"),
                  r.get("actor_slack_id")):
            if isinstance(v, str) and v.startswith("U"):
                ids.add(v)
    names: dict[str, str] = {}
    if ids:
        for nr in db.fetch_all(
                "SELECT slack_user_id, name FROM requesters "
                "WHERE slack_user_id = ANY(%s)", (list(ids),)):
            if nr.get("name"):
                names[nr["slack_user_id"]] = nr["name"]

    def _name_of(slack_id):
        return names.get(slack_id, slack_id) if slack_id else slack_id

    out = []
    for r in rows:
        k = mapping.AUDIT_KIND.get(r["action"], "other")
        if kind and k != kind:
            continue
        out.append(mapping.admin_audit_entry(r, k, _alias_of, _name_of))
    return {"audit": out}


@router.get("/metrics")
def admin_metrics(claims: dict = Depends(deps.current_user)):
    """Full product metrics from p_metrics_request_facts (reportable,
    self-test-excluded) + aux views — the same panels as the static S3
    dashboard, aggregated server-side. See web/metrics.py."""
    admin.require_admin(claims, "review")
    return metrics.build_metrics()


@router.get("/feedback")
def admin_feedback(claims: dict = Depends(deps.current_user)):
    """Post-run ratings + comments (request_ratings_reportable)."""
    admin.require_admin(claims, "review")
    rows = db.fetch_all(
        "SELECT r.id, r.request_id, r.slack_user_id, r.rating, r.feedback_text, "
        "  r.rated_at, req.name FROM request_ratings_reportable r "
        "LEFT JOIN requesters req ON req.slack_user_id = r.slack_user_id "
        "ORDER BY r.rated_at DESC LIMIT 100")
    return {"feedback": [mapping.feedback_entry(r) for r in rows]}
