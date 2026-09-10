"""Admin-panel endpoints (ADMIN_API.md): approval queue, decisions,
batch approve, and the kill switch.

Every route runs behind verify_session (deps.current_user) then
require_admin. Decisions reuse core_decide — the SAME pipeline the Slack
approval buttons run — so a web decision performs the identical DB
transition + audit row + Slack mirror + executor dispatch. The web panel
is an alternative surface, never a parallel or bypass path.
"""
from __future__ import annotations

import json
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
    pii,
    requesters,
    schema_catalog,
    targets,
    teams,
)
from .. import config as cfg
from .. import teams as teams_mod
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
            "SELECT g.slack_user_id AS subject, "
            # A principal is a requester or an admin, sometimes both. Resolving
            # against `requesters` alone meant an admin-only subject rendered
            # as a raw Slack handle beside rows that rendered as names. No
            # grant is in that state today, which is exactly why it would have
            # been found by someone reading a screen rather than by a test.
            "       COALESCE(r.name, a.name) AS subject_name, "
            "  g.target_server_id, g.allowed_databases, g.mode, "
            "  g.granted_by, g.granted_at, g.expires_at, "
            # Resolved the same way as the subject, so the Granted-by column
            # stops showing a handle beside an auto-approve table that shows a
            # name. Falls back to the raw value, which for a handful of legacy
            # rows is a free-text note rather than a principal id.
            "       COALESCE(gr.name, ga.name) AS granted_by_name "
            "FROM user_target_grants g "
            "LEFT JOIN requesters r  ON r.slack_user_id  = g.slack_user_id "
            "LEFT JOIN admins     a  ON a.slack_user_id  = g.slack_user_id "
            "LEFT JOIN requesters gr ON gr.slack_user_id = g.granted_by "
            "LEFT JOIN admins     ga ON ga.slack_user_id = g.granted_by "
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


def _connection_payload(row: dict, databases: list[str] | None = None) -> dict:
    from . import routes_data
    if databases is None:
        databases = routes_data._catalog_databases(row["id"])
    return _connection_entry(row, databases)


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
    # Who is responsible for each one (migration 115). A target used to be an
    # independent object and the only link to a team was a hostname string
    # joined at query time — which meant this screen could not answer "whose
    # database is this", the question an admin brings to it before deciding
    # anything else. Many-to-many, because six of these are shared between two
    # teams, and `syncedFrom` because a synced link is not the reader's to
    # correct here while a hand-made one is.
    owners: dict[int, list[dict]] = {}
    for row in db.fetch_all(
            "SELECT tt.target_id, t.id, t.name, t.display_name, tt.source "
            "  FROM target_team tt "
            "  JOIN team t ON t.id = tt.team_id AND NOT t.is_deleted "
            " ORDER BY t.display_name"):
        owners.setdefault(row["target_id"], []).append(
            {"id": row["id"], "name": row["name"],
             "displayName": row["display_name"], "syncedFrom": row["source"]})
    from . import routes_data
    rows = targets.list_admin_rows()
    # One read for the whole fleet. This asked per target, which is a round
    # trip per row on the screen whose job is to show every row: 117 targets,
    # 121 statements.
    catalog_dbs = routes_data._catalog_databases_map([r["id"] for r in rows])
    out = []
    for r in rows:
        payload = _connection_payload(r, catalog_dbs.get(r["id"], []))
        payload["owners"] = owners.get(r["id"], [])
        out.append(payload)
    return {"connections": out}


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
    """Existing team by (case-insensitive) name, from whichever model is
    authoritative. Teams are created deliberately (SQL / Slack / an org
    import), never from the web — this only looks one up.

    Matches `display_name` as well as `name` under the new model: a pod's code
    is `team-a` and what everyone calls it is `Team A`, and an admin
    typing what the screen shows should not get "no such team".
    """
    n = (name or "").strip()
    if not n:
        return None
    if teams_mod.use_v2():
        return db.fetch_one(
            "SELECT id, COALESCE(display_name, name) AS name FROM team "
            " WHERE NOT is_deleted "
            "   AND (lower(name) = lower(%s) OR lower(display_name) = lower(%s)) "
            " LIMIT 1", (n, n))
    return db.fetch_one(
        "SELECT id, name FROM teams WHERE lower(name) = lower(%s)", (n,))


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
    # One person or several, in the same call. `subject` stays for the single
    # case rather than being replaced: every existing caller sends it, and a
    # required rename is a breaking change bought for nothing. Exactly one of
    # the two is used — `subjects` when it is non-empty, otherwise `subject`.
    subject: str | None = None
    subjects: list[str] | None = None
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
        # A team is already a set of people, so `subjects` has no meaning here
        # and a list of them would silently grant to only the first.
        named = [x for x in (body.subjects or []) if x] or (
            [body.subject] if body.subject else [])
        if len(named) != 1:
            raise deps._error(400, "bad_request",
                              "Name exactly one team. A team grant already "
                              "covers everyone in it.")
        team = _resolve_team(named[0])
        if team is None:
            raise deps._error(404, "not_found",
                              f"No team named '{named[0]}'. Create the team "
                              "in Slack / SQL first — teams aren't created here.")
        # Where the row goes depends on which model is authoritative, and
        # under the new one it CANNOT go to the legacy table: a pod team has
        # no `teams` row for `team_target_grants.team_id` to reference. After
        # the pod cutover that table is empty, so this branch had no way to
        # grant a team anything at all — the 27 grants the cutover wrote went
        # straight to `access_grant` and there was no supported path to the
        # 28th.
        #
        # Either way the auth-event trigger on the table written DMs every
        # affected member (migration 060 for the legacy one, 108 for the new).
        with db.transaction() as cur:
            if teams_mod.use_v2():
                # One row per database, and a row is immutable: changing the
                # tier is a revoke plus an insert, or `access_grant_live_uq`
                # would hold both the old and the new at once and the
                # resolver would take the more permissive of the two.
                cur.execute(
                    "UPDATE access_grant SET revoked_at = NOW() "
                    " WHERE team_id = %s AND target_id = %s "
                    "   AND NOT auto_approve AND revoked_at IS NULL "
                    "   AND NOT is_deleted", (team["id"], tid))
                for dbn in (dbs or [None]):
                    cur.execute(
                        "INSERT INTO access_grant "
                        "  (team_id, target_id, all_targets, database_name, "
                        "   all_databases, tier, valid_from, valid_until, reason) "
                        "VALUES (%s,%s,FALSE,%s,%s,%s,now(),%s,%s)",
                        (team["id"], tid, dbn, dbn is None, tier, expires_at,
                         "granted from the admin panel"))
            else:
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
        # A team's subjectName IS its name — same rule the grant list follows,
        # so a client can read subjectName on every row without branching.
        return {"id": f"t:{team['id']}:{tid}", "subjectType": "team",
                "subject": team["name"], "subjectName": team["name"],
                "connectionId": body.connectionId,
                "databases": dbs or "*", "tier": tier.upper()}

    subjects = [x for x in (body.subjects or []) if x] or (
        [body.subject] if body.subject else [])
    if not subjects:
        raise deps._error(400, "bad_request",
                          "Name at least one subject.")
    # Every id is checked BEFORE anything is written, and the message names the
    # one that failed. Granting to five people is one act: "three of the five
    # were written" is not a state an operator can act on, because the grants
    # table records what exists and never what was meant.
    seen: set[str] = set()
    ordered: list[str] = []
    for sid in subjects:
        if not _valid_principal(sid):
            raise deps._error(
                400, "bad_request",
                f"{sid} is not a principal id (expected a Slack user id or "
                f"local:<username>). Nothing was written.")
        if sid not in seen:
            seen.add(sid)
            ordered.append(sid)

    written = grants.grant_many(
        granter_id=uid, granter_name=claims.get("name"),
        grantees=[(sid, _slack_profile(sid)) for sid in ordered],
        target_id=tid, mode=tier, databases=dbs, reason=body.reason,
        notify=True, expires_at=expires_at)
    summary = written[0]
    body_subject = ordered[0]
    # `subjectName` so the caller can land on the person it just created.
    # Granting is what brings someone into QueryHub, so this response is the
    # first moment their name exists anywhere — without it the client has to
    # refetch the whole people list and find them by handle.
    row = db.fetch_one(
        "SELECT COALESCE(r.name, a.name) AS name "
        "  FROM (SELECT %s AS pid) p "
        "  LEFT JOIN requesters r ON r.slack_user_id = p.pid "
        "  LEFT JOIN admins a ON a.slack_user_id = p.pid",
        (body_subject,))
    return {"id": f"u:{body_subject}:{tid}", "subjectType": "user",
            "subject": body_subject,
            # Everyone the call wrote, in the order asked. The single-subject
            # fields above stay exactly as they were so an existing client
            # keeps working unchanged.
            "subjects": ordered,
            "subjectName": (row or {}).get("name"),
            "connectionId": body.connectionId,
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
    """Every principal an admin can name — the pool the Teams screen assigns
    from and the subject pickers list. `handle` == slack_user_id so it lines up
    1:1 with grant subjects.

    Four sources, not one. It used to be `requesters WHERE enabled`, which left
    a picker that could not offer:

      * a DISABLED requester — the offboarded person whose grants an admin has
        come to the screen to clean up;
      * an ADMIN with no requesters row — a DBA who only ever approves;
      * an identity that exists only as the subject of a grant or an
        auto-approve row.

    A name missing from the list is not a name an admin gives up on: they type
    the id by hand, and a typo there writes a grant against a principal that
    cannot sign in. So everyone is listed, and `enabled` says which are live —
    a flag the client can sort and mark by, rather than an absence it has to
    interpret. Not paginated: this is a directory of tens, and a page boundary
    in a picker is a person you cannot find.
    """
    admin.require_admin(claims, "access")
    # The UNION is wrapped rather than ordered in place: a set operation can
    # only ORDER BY output column NAMES, so the sort key has to sit outside it.
    rows = db.fetch_all(
        "SELECT * FROM ("
        "  SELECT slack_user_id, name, enabled, 'requester' AS kind"
        "    FROM requesters"
        "   UNION ALL"
        "  SELECT slack_user_id, name, enabled, 'admin'"
        "    FROM admins a"
        "   WHERE NOT EXISTS (SELECT 1 FROM requesters r"
        "                      WHERE r.slack_user_id = a.slack_user_id)"
        "   UNION ALL"
        # Subjects of a grant with no row of their own: they hold access, so an
        # access screen that cannot name them is the one place they must appear.
        "  SELECT g.slack_user_id, NULL, FALSE, 'grant_only'"
        "    FROM (SELECT DISTINCT slack_user_id FROM user_target_grants"
        "           WHERE revoked_at IS NULL"
        "           UNION"
        "          SELECT DISTINCT slack_user_id FROM auto_approve_grants) g"
        "   WHERE NOT EXISTS (SELECT 1 FROM requesters r"
        "                      WHERE r.slack_user_id = g.slack_user_id)"
        "     AND NOT EXISTS (SELECT 1 FROM admins a"
        "                      WHERE a.slack_user_id = g.slack_user_id)"
        ") p ORDER BY p.enabled DESC, lower(coalesce(p.name, p.slack_user_id))")
    return {"people": [{
        "id": r["slack_user_id"], "handle": r["slack_user_id"],
        "slackId": r["slack_user_id"], "name": r["name"] or r["slack_user_id"],
        "initials": _initials(r["name"], r["slack_user_id"]),
        # Live or not, and why they are in the list at all. The client sorts
        # and marks by these; nothing is inferred from the row being present.
        "enabled": bool(r["enabled"]),
        "kind": r["kind"],
    } for r in rows]}


def _teams_payload() -> list[dict]:
    """Every team with its members, from whichever model is authoritative.

    This read the legacy `teams` table directly, on the reasoning that the
    team CRUD beside it writes there too — read what you write. That held
    until the company moved to the pod structure: the legacy rows were
    removed, the teams now live only in the nine-table model, and the screen
    went from six teams to none while `/sql teams` showed thirteen. Two
    surfaces, one question, opposite answers.

    So it follows `access_model_v2` like the resolver and the Slack views do.
    `source` rides along because a synced team is not the reader's to rename
    here — the next sync would put the name back.
    """
    if teams_mod.use_v2():
        rows = db.fetch_all(
            "SELECT t.id, t.name, t.display_name, t.description, t.source "
            "  FROM team t WHERE NOT t.is_deleted ORDER BY lower(t.name)")
        members = db.fetch_all(
            "SELECT m.team_id, i.external_id AS slack_user_id "
            "  FROM team_member m "
            "  JOIN principal_identity i ON i.principal_id = m.principal_id "
            "   AND i.provider = 'slack' AND NOT i.is_deleted "
            " WHERE NOT m.is_deleted")
        by_team: dict[int, list[str]] = {}
        for m in members:
            by_team.setdefault(m["team_id"], []).append(m["slack_user_id"])
        return [{
            "id": str(t["id"]),
            "name": t["display_name"] or t["name"],
            "desc": t["description"] or "",
            "members": by_team.get(t["id"], []), "subteams": [],
            "syncedFrom": t["source"] if t["source"] != "manual" else None,
        } for t in rows]

    legacy = db.fetch_all(
        "SELECT id, name, description FROM teams ORDER BY lower(name)")
    members = db.fetch_all("SELECT team_id, slack_user_id FROM team_members")
    by_team = {}
    for m in members:
        by_team.setdefault(m["team_id"], []).append(m["slack_user_id"])
    # subteams is always [] — the grant model has no team nesting (see teams.py
    # effective_grant_for_user), so the web UI does not offer it.
    return [{
        "id": str(t["id"]), "name": t["name"], "desc": t["description"] or "",
        "members": by_team.get(t["id"], []), "subteams": [],
        "syncedFrom": None,
    } for t in legacy]


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


def _team_for_write(team_id: int) -> dict:
    """The team this id names, in whichever model the screen is reading.

    The Teams list started answering from the nine-table model when the read
    path followed the switch, but the three mutation routes kept addressing
    the legacy `teams` table by the id the list had handed out. Those are
    different tables with different sequences: after the pod cutover the list
    returns ids 14-26 while `teams` is empty and its sequence sits at 10. So a
    rename reported success and changed nothing, a delete answered 404 for
    every team on the screen, and once four teams had been created through the
    web the two id spaces would have started overlapping -- at which point the
    rename would have edited a DIFFERENT team than the one on screen.

    Returns the row plus `source`, which the callers use to refuse editing a
    team this screen does not own.
    """
    if teams_mod.use_v2():
        return db.fetch_one(
            "SELECT id, name, display_name, source FROM team "
            " WHERE id = %s AND NOT is_deleted", (team_id,))
    row = db.fetch_one("SELECT id, name FROM teams WHERE id = %s", (team_id,))
    return {**row, "display_name": None, "source": None} if row else None


def _refuse_synced_team(team: dict) -> None:
    """A team that arrived from an org import is not this screen's to edit.

    `scripts/import_teams.py` and the pod sync own every row carrying their
    `source`, and reconcile it on the next run -- so a rename here would be
    silently undone, which is worse than being refused. Same rule the
    connection-owner rows already follow: a synced link is not the reader's to
    correct, a hand-made one is.
    """
    if team.get("source"):
        raise deps._error(
            409, "synced_team",
            f"This team is kept in step with '{team['source']}' and cannot be "
            f"edited here; change it at the source.")


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
        if teams_mod.use_v2():
            # `source` stays NULL: a team made here is hand-made, and the
            # importers only reconcile rows carrying their own source.
            cur.execute(
                "INSERT INTO team (name, display_name, description) "
                "VALUES (%s, %s, %s) RETURNING id",
                (name, name, (body.desc or "").strip() or None))
            tid = cur.fetchone()["id"]
            for m in members:
                cur.execute(
                    "INSERT INTO team_member (team_id, principal_id) "
                    "SELECT %s, i.principal_id FROM principal_identity i "
                    " WHERE i.external_id = %s AND i.provider = 'slack' "
                    "   AND NOT i.is_deleted "
                    "ON CONFLICT DO NOTHING", (tid, m))
        else:
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
    team = _team_for_write(team_id)
    if team is None:
        raise deps._error(404, "not_found", "No such team.")
    _refuse_synced_team(team)
    name = (body.name or "").strip()
    if not name:
        raise deps._error(400, "bad_request", "Team name is required.")
    if name.lower() != team["name"].lower() and _resolve_team(name):
        raise deps._error(409, "conflict", f"A team named '{name}' already exists.")
    desired = set(_valid_member_ids(body.members))
    with db.transaction() as cur:
        if teams_mod.use_v2():
            cur.execute(
                "UPDATE team SET name = %s, display_name = %s, description = %s "
                " WHERE id = %s", (name, name,
                                   (body.desc or "").strip() or None, team_id))
            cur.execute(
                "SELECT i.external_id AS slack_user_id FROM team_member m "
                "  JOIN principal_identity i ON i.principal_id = m.principal_id "
                "   AND i.provider = 'slack' AND NOT i.is_deleted "
                " WHERE m.team_id = %s AND NOT m.is_deleted", (team_id,))
            existing = {r["slack_user_id"] for r in cur.fetchall()}
            to_add, to_remove = desired - existing, existing - desired
            if to_remove:
                # Soft delete, like every other reader of this table expects:
                # a membership that ended is a fact somebody may be reading a
                # message about.
                cur.execute(
                    "UPDATE team_member m SET is_deleted = TRUE "
                    "  FROM principal_identity i "
                    " WHERE m.team_id = %s AND i.principal_id = m.principal_id "
                    "   AND i.provider = 'slack' AND NOT i.is_deleted "
                    "   AND i.external_id = ANY(%s)", (team_id, list(to_remove)))
            for m in to_add:
                cur.execute(
                    "INSERT INTO team_member (team_id, principal_id) "
                    "SELECT %s, i.principal_id FROM principal_identity i "
                    " WHERE i.external_id = %s AND i.provider = 'slack' "
                    "   AND NOT i.is_deleted "
                    # `team_member_uq` is PARTIAL (WHERE NOT is_deleted), so
                    # the inference has to carry the same predicate. A member
                    # removed earlier does not conflict at all and gets a fresh
                    # row, which is what soft delete is for -- the old row
                    # stays as the record that the membership once ended.
                    "ON CONFLICT (team_id, principal_id) WHERE NOT is_deleted "
                    "DO NOTHING", (team_id, m))
        else:
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
    team = _team_for_write(team_id)
    if team is None:
        raise deps._error(404, "not_found", "No such team.")
    _refuse_synced_team(team)
    with db.transaction() as cur:
        if teams_mod.use_v2():
            # Soft delete here, and the grants with it: `access_grant` rows
            # naming this team keep answering otherwise, and the resolver does
            # not join `team.is_deleted`.
            cur.execute("UPDATE team SET is_deleted = TRUE WHERE id = %s",
                        (team_id,))
            cur.execute("UPDATE team_member SET is_deleted = TRUE "
                        " WHERE team_id = %s AND NOT is_deleted", (team_id,))
            cur.execute("UPDATE access_grant SET revoked_at = NOW() "
                        " WHERE team_id = %s AND revoked_at IS NULL "
                        "   AND NOT is_deleted", (team_id,))
        else:
            # team_members + team_target_grants cascade on delete; their
            # auth_event triggers DM every affected member.
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
    # Same seam as the three team routes: the People tab lists teams from
    # whichever model is live, so the ids it sends back have to be checked
    # against that model. Validated against `teams` alone, every id from a v2
    # screen fell out here and the save quietly set the person's membership to
    # nothing.
    v2 = teams_mod.use_v2()
    if desired:  # keep only teams that actually exist
        rows = db.fetch_all(
            "SELECT id FROM team WHERE id = ANY(%s) AND NOT is_deleted"
            if v2 else "SELECT id FROM teams WHERE id = ANY(%s)",
            (list(desired),))
        desired = {r["id"] for r in rows}
    with db.transaction() as cur:
        if v2:
            cur.execute(
                "SELECT m.team_id FROM team_member m "
                "  JOIN principal_identity i ON i.principal_id = m.principal_id "
                "   AND i.provider = 'slack' AND NOT i.is_deleted "
                " WHERE i.external_id = %s AND NOT m.is_deleted", (slack_id,))
            existing = {r["team_id"] for r in cur.fetchall()}
            to_add, to_remove = desired - existing, existing - desired
            if to_remove:
                cur.execute(
                    "UPDATE team_member m SET is_deleted = TRUE "
                    "  FROM principal_identity i "
                    " WHERE i.principal_id = m.principal_id "
                    "   AND i.provider = 'slack' AND NOT i.is_deleted "
                    "   AND i.external_id = %s AND m.team_id = ANY(%s) "
                    "   AND NOT m.is_deleted", (slack_id, list(to_remove)))
            for tid in to_add:
                cur.execute(
                    "INSERT INTO team_member (team_id, principal_id) "
                    "SELECT %s, i.principal_id FROM principal_identity i "
                    " WHERE i.external_id = %s AND i.provider = 'slack' "
                    "   AND NOT i.is_deleted "
                    "ON CONFLICT (team_id, principal_id) WHERE NOT is_deleted "
                    "DO NOTHING", (tid, slack_id))
        else:
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
    # 'merge' adds to what the person already has; 'replace' makes their access
    # match the source exactly, which means REVOKING what the source lacks.
    # Merge is the default because it is the one that cannot take anything away.
    mode: str = "merge"
    # Auto-approve skips human review, so it never rides along with a copy by
    # accident: off unless the caller says otherwise.
    includeAutoApprove: bool = False
    # Report what a replace WOULD revoke, and write nothing. The screen shows
    # those rows by name before anyone confirms a destructive copy.
    dryRun: bool = False


def _alias_map(cur, ids) -> dict[int, str]:
    """id -> alias for a set of targets, in one query on the caller's cursor.

    The response names servers rather than ids: an admin confirming a
    destructive copy reads a server name, not "target 12". Resolved through
    the cursor already in hand instead of `targets.get` per id — same answer,
    one round trip, and a caller that has faked the cursor has faked this."""
    ids = [int(i) for i in ids if i is not None]
    if not ids:
        return {}
    cur.execute("SELECT id, alias FROM target_servers WHERE id = ANY(%s)", (ids,))
    return {r["id"]: r["alias"] for r in (cur.fetchall() or [])}


class _CopyPreview(Exception):
    """A dry run's answer, raised so the transaction it was computed in rolls
    back. The preview has to see the same rows the write would — including the
    requesters upsert that happens first — and the only way to look at them
    without keeping them is to leave by an exception."""

    def __init__(self, payload: dict):
        super().__init__("copy-access dry run")
        self.payload = payload


@router.get("/people/resolve")
def admin_resolve_person(principal: str | None = None,
                         claims: dict = Depends(deps.current_user)):
    """Who is this principal id — before anything is written.

    Adding a person is not a create: granting them access IS the create
    (`POST /grants` whitelists an id QueryHub has never seen, filling name /
    email / tz from their Slack profile). What a form cannot do without this is
    show WHO it is about to grant, so a mistyped id is caught after the grant
    exists rather than before.

    `known` says whether they already have a row here, so the caller can offer
    "grant" or "edit" rather than guessing.
    """
    # `principal` is optional in the SIGNATURE on purpose: FastAPI validates
    # query params before the handler runs, so a required one answers 422 to a
    # non-admin — before the admin gate, which is the one thing every route
    # under /api/admin must do first (tests/test_admin_routes_gated.py).
    admin.require_admin(claims, "access")
    pid = (principal or "").strip()
    if not _valid_principal(pid):
        raise deps._error(400, "bad_request",
                          "principal must be a Slack user id or local:<username>.")
    row = db.fetch_one(
        "SELECT r.slack_user_id, r.name, r.email, r.enabled, "
        "       (a.slack_user_id IS NOT NULL) AS is_admin "
        "  FROM requesters r "
        "  LEFT JOIN admins a ON a.slack_user_id = r.slack_user_id "
        " WHERE r.slack_user_id = %s", (pid,))
    if row is None:
        row = db.fetch_one(
            "SELECT slack_user_id, name, NULL AS email, enabled, TRUE AS is_admin "
            "  FROM admins WHERE slack_user_id = %s", (pid,))
    if row is not None:
        return {"principal": pid, "known": True, "name": row["name"],
                "email": row["email"], "enabled": bool(row["enabled"]),
                "admin": bool(row["is_admin"])}
    prof = _slack_profile(pid) if pid.startswith("U") else {}
    return {"principal": pid, "known": False, "name": prof.get("name"),
            "email": prof.get("email"), "enabled": False, "admin": False}


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
    mode_in = (body.mode or "merge").strip().lower()
    if mode_in not in ("merge", "replace"):
        raise deps._error(400, "bad_request",
                          "mode must be 'merge' or 'replace'.")
    replace = mode_in == "replace"
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
    team_names: list[str] = []

    try:
      with db.transaction() as cur:
          # Whitelist the destination FIRST. This endpoint writes team_members and
          # user_target_grants directly, so pointing it at a Slack id QueryHub has
          # never seen produced grant rows for someone with no `requesters` row:
          # every submission still refused (the whitelist gate), and the person
          # invisible in the people list — grants that look right and do nothing.
          # `grants.grant` has always done this; the copy path had to as well.
          # A profile lookup fills the name so the row is not just an id, and the
          # upsert never downgrades an existing person.
          prof = _slack_profile(slack_id)
          cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
          cur.execute(
              "INSERT INTO requesters (slack_user_id, name, email, tz, enabled, "
              "                        added_at, added_by) "
              "VALUES (%s, %s, %s, %s, TRUE, NOW(), %s) "
              "ON CONFLICT (slack_user_id) DO UPDATE "
              "  SET enabled = TRUE, "
              "      name  = COALESCE(requesters.name,  EXCLUDED.name), "
              "      email = COALESCE(requesters.email, EXCLUDED.email), "
              "      tz    = COALESCE(requesters.tz,    EXCLUDED.tz)",
              (slack_id, prof.get("name"), prof.get("email"), prof.get("tz"),
               uid))
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
              # Copy the memberships from the model that holds them. Against
              # the legacy tables this copied nothing at all after the pod
              # cutover, and said so in the past tense.
              if teams_mod.use_v2():
                  cur.execute(
                      "INSERT INTO team_member (team_id, principal_id) "
                      "SELECT m.team_id, dst.principal_id "
                      "  FROM team_member m "
                      "  JOIN principal_identity src "
                      "    ON src.principal_id = m.principal_id "
                      "   AND src.provider = 'slack' AND NOT src.is_deleted "
                      "  CROSS JOIN principal_identity dst "
                      " WHERE src.external_id = %s AND NOT m.is_deleted "
                      "   AND dst.external_id = %s AND dst.provider = 'slack' "
                      "   AND NOT dst.is_deleted "
                      "ON CONFLICT (team_id, principal_id) WHERE NOT is_deleted "
                      "DO NOTHING RETURNING team_id",
                      (body.source, slack_id))
              else:
                  cur.execute(
                      "INSERT INTO team_members (team_id, slack_user_id) "
                      "SELECT team_id, %s FROM team_members WHERE slack_user_id = %s "
                      "ON CONFLICT DO NOTHING RETURNING team_id",
                      (slack_id, body.source))
              teams_joined = [r["team_id"] for r in cur.fetchall()]
              # Names, not just ids: the caller's confirmation says "joined
              # petrels, platform", and it cannot build that from integers.
              if teams_joined:
                  cur.execute(
                      "SELECT COALESCE(display_name, name) AS name FROM team "
                      " WHERE id = ANY(%s) ORDER BY 1"
                      if teams_mod.use_v2() else
                      "SELECT name FROM teams WHERE id = ANY(%s) ORDER BY name",
                      (teams_joined,))
                  team_names = [r["name"] for r in cur.fetchall()]
              to_write = src_grants
          else:
              # Team-derived targets become explicit grants. A user row supersedes
              # the team's for that target, so where both exist the source's own
              # grant wins — it is the narrower, deliberate one.
              merged = {r["target_server_id"]: r for r in team_grants}
              merged.update(src_grants)
              to_write = merged

          # REPLACE means the person ends up with the source's access and
          # nothing else — so it has to REVOKE what the source does not have.
          # Worked out before anything is written, and reported by target ALIAS
          # rather than id: an admin confirming a destructive copy is reading
          # server names, and "revokes 3 grants" is not a sentence anyone can
          # check.
          revoked: list[dict] = []
          if replace:
              cur.execute(
                  "SELECT target_server_id, mode FROM user_target_grants "
                  " WHERE slack_user_id = %s AND revoked_at IS NULL", (slack_id,))
              for r in cur.fetchall():
                  tid = r["target_server_id"]
                  if tid in to_write or tid in forbidden:
                      continue
                  revoked.append({"targetId": tid,
                                  "tier": (r["mode"] or "ro").upper()})

          # A dry run answers the same question and writes nothing — the screen
          # asks it to name the rows before the admin confirms. Raising here
          # rolls the transaction back, including the requesters upsert above.
          if body.dryRun:
              # The preview says how many auto-approve windows would come
              # across. Counted here rather than left out: the panel draws a
              # line from it, and an absent field draws silence — which reads
              # as "none" for the one option that skips human review.
              would_auto = 0
              if body.includeAutoApprove:
                  cur.execute(
                      "SELECT count(*) AS n FROM auto_approve_grants "
                      " WHERE slack_user_id = %s "
                      "   AND (expires_at IS NULL OR expires_at > NOW())",
                      (body.source,))
                  would_auto = int((cur.fetchone() or {}).get("n") or 0)
              raise _CopyPreview({
                  "wouldCopyAutoApprove": would_auto,
                  "dryRun": True, "slackId": slack_id, "copiedFrom": body.source,
                  "mode": "replace" if replace else "merge",
                  "wouldWrite": [
                      _alias_map(cur, sorted(set(to_write) - forbidden)).get(t, str(t))
                      for t in sorted(set(to_write) - forbidden)],
                  "wouldRevoke": [
                      dict(r, connectionId=_alias_map(cur, [r["targetId"]])
                           .get(r["targetId"], str(r["targetId"])))
                      for r in revoked],
                  "wouldJoinTeams": team_names,
                  "tier": tier})

          written: list[int] = []
          for tid, g in sorted(to_write.items()):
              if tid in forbidden:
                  continue
              cur.execute(
                  "INSERT INTO user_target_grants "
                  "  (slack_user_id, target_server_id, allowed_databases, mode, granted_by) "
                  "VALUES (%s, %s, %s, %s, %s) "
                  # On replace the row must END UP matching the source, so an
                  # existing grant is overwritten rather than left alone. Merge
                  # keeps the old DO NOTHING: it adds, it does not rewrite what
                  # somebody already decided.
                  + ("ON CONFLICT (slack_user_id, target_server_id) DO UPDATE "
                     "  SET allowed_databases = EXCLUDED.allowed_databases, "
                     "      mode = EXCLUDED.mode, granted_by = EXCLUDED.granted_by, "
                     "      granted_at = NOW(), revoked_at = NULL"
                     if replace else
                     "ON CONFLICT (slack_user_id, target_server_id) DO NOTHING"),
                  (slack_id, tid, g["allowed_databases"], tier or g["mode"], uid))
              if cur.rowcount:
                  written.append(tid)

          for r in revoked:
              cur.execute(
                  "UPDATE user_target_grants SET revoked_at = NOW() "
                  " WHERE slack_user_id = %s AND target_server_id = %s "
                  "   AND revoked_at IS NULL", (slack_id, r["targetId"]))

          # Auto-approve is a separate table and a separate decision: it skips
          # human review, so it is copied only when asked for.
          auto_copied: list[str] = []
          if body.includeAutoApprove:
              cur.execute(
                  "SELECT target_server_id, database_name, max_tier, expires_at "
                  "  FROM auto_approve_grants "
                  " WHERE slack_user_id = %s "
                  "   AND (expires_at IS NULL OR expires_at > NOW())",
                  (body.source,))
              for r in cur.fetchall():
                  if r["target_server_id"] in forbidden:
                      continue
                  cur.execute(
                      "INSERT INTO auto_approve_grants "
                      "  (slack_user_id, target_server_id, database_name, "
                      "   max_tier, granted_by, expires_at) "
                      "VALUES (%s, %s, %s, %s, %s, %s)",
                      (slack_id, r["target_server_id"], r["database_name"],
                       r["max_tier"], uid, r["expires_at"]))
                  auto_copied.append((r["target_server_id"], r["database_name"]))

          # Every id the response will name, resolved in one query.
          names = _alias_map(cur, set(written)
                             | {r["targetId"] for r in revoked}
                             | {t for t, _ in auto_copied})
          written_names = [names.get(t, str(t)) for t in written]
          for r in revoked:
              r["connectionId"] = names.get(r["targetId"], str(r["targetId"]))
          auto_names = [(names.get(t) or "all targets")
                        + (f"/{d}" if d else "") for t, d in auto_copied]

          audit.log_in(cur, None, uid, claims.get("name"), "access_copied",
                       {"to": slack_id, "from": body.source,
                        "mode": "replace" if replace else "merge",
                        "targets_granted": written,
                        "targets_revoked": [r["targetId"] for r in revoked],
                        "auto_approve_copied": auto_names,
                        "teams_joined": teams_joined,
                        "include_teams": body.includeTeams,
                        "tier_override": tier,
                        "skipped_control_plane": sorted(
                            set(to_write) & forbidden)})

    except _CopyPreview as preview:
        return preview.payload

    return {"slackId": slack_id, "copiedFrom": body.source,
            "mode": "replace" if replace else "merge",
            "targetsGranted": len(written), "teamsJoined": len(teams_joined),
            # `written` and `teams` are what the UI reads to say what happened.
            # Kept alongside the counts rather than replacing them: the counts
            # are what the audit row and any script would want.
            "written": len(written), "writtenTargets": written_names,
            "teams": team_names,
            # Named, not counted: a revoke the admin cannot see is a revoke
            # they cannot check.
            "revoked": revoked,
            "autoApproveCopied": len(auto_names),
            "autoApproveCopiedTargets": auto_names,
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

    # Read from the model that holds the memberships. This is a security
    # screen -- an admin asks it what somebody can reach before deciding -- and
    # against the legacy tables it answered "no teams" for everybody once the
    # pod cutover emptied them.
    teams_of = db.fetch_all(
        "SELECT t.id, COALESCE(t.display_name, t.name) AS name "
        "  FROM team_member m JOIN team t ON t.id = m.team_id "
        "  JOIN principal_identity i ON i.principal_id = m.principal_id "
        "   AND i.provider = 'slack' AND NOT i.is_deleted "
        " WHERE i.external_id = %s AND NOT m.is_deleted AND NOT t.is_deleted "
        " ORDER BY 2"
        if teams_mod.use_v2() else
        "SELECT t.id, t.name FROM team_members m JOIN teams t ON t.id = m.team_id "
        " WHERE m.slack_user_id = %s ORDER BY t.name", (slack_id,))

    # Which team supplied a team-sourced grant, and when each one ends. The
    # resolver deliberately answers neither — it runs on every submission and
    # returns the decision, not its provenance. Both are looked up here, in two
    # queries rather than two per target, because "via <team>" with no team name
    # is a label that says nothing and an expiry the panel cannot see is the
    # thing that will surprise someone.
    own_exp = {r["target_server_id"]: r["expires_at"] for r in db.fetch_all(
        "SELECT target_server_id, expires_at FROM user_target_grants "
        " WHERE slack_user_id = %s AND revoked_at IS NULL", (slack_id,))}
    via_team: dict[int, dict] = {}
    for r in db.fetch_all(
            "SELECT g.target_server_id, t.name, g.expires_at "
            "  FROM team_target_grants g "
            "  JOIN teams t ON t.id = g.team_id "
            "  JOIN team_members m ON m.team_id = g.team_id "
            " WHERE m.slack_user_id = %s AND g.revoked_at IS NULL "
            "   AND (g.expires_at IS NULL OR g.expires_at > NOW()) "
            " ORDER BY t.name", (slack_id,)):
        # First team wins for the label. Several can grant the same target and
        # the resolver has already merged them into one decision, so naming one
        # is a simplification — but naming none is worse, and naming all of them
        # turns a row into a list.
        via_team.setdefault(r["target_server_id"], r)

    # One resolution pass for every target rather than one call per target.
    # The per-target version asked four queries each, so this screen cost 449
    # round trips and 780ms at p95 — and it is refreshed every time the admin
    # picks a different person. `effective_grants_for_user` answers the same
    # question with the same precedence; a test compares the two.
    all_targets = targets.list_all()
    grants_by_target = teams.effective_grants_for_user(
        slack_id, [t.id for t in all_targets])
    alias_by_id = {t.id: t.alias for t in all_targets}

    out = []
    for t in all_targets:
        g = grants_by_target.get(t.id)
        if g is None:
            continue
        src = g.get("source")
        team = via_team.get(t.id) if src == "team" else None
        out.append({
            "connectionId": t.alias,
            "enabled": t.enabled,
            "tier": (g.get("mode") or "ro").upper(),
            # NULL means every database on the target, which is not the same
            # as an empty list and must not render as "no databases".
            "databases": sorted(g["allowed_databases"]) if g.get("allowed_databases") else None,
            "allDatabases": g.get("allowed_databases") is None,
            # 'user' | 'team' | 'admin_or_bypass' — the third is an admin, who
            # reaches everything without a row anywhere.
            "source": src,
            "sourceTeam": (team or {}).get("name"),
            "expiresAt": mapping.iso(
                (team or {}).get("expires_at") if src == "team" else own_exp.get(t.id)),
        })

    auto = db.fetch_all(
        "SELECT max_tier, target_server_id, database_name, expires_at "
        "  FROM auto_approve_grants "
        " WHERE slack_user_id = %s AND starts_at <= NOW() "
        "   AND (expires_at IS NULL OR expires_at > NOW())", (slack_id,))

    # Standing as an APPROVER, which is a different question from what they can
    # query and is invisible in the grant tables. Leaving it out made the view
    # answer "what can they read" when the question asked is usually "what can
    # this person do here".
    adm = db.fetch_one(
        "SELECT max_tier, scope_team_ids, scope_target_ids, can_grant, enabled "
        "  FROM admins WHERE slack_user_id = %s", (slack_id,))

    # A per-person result cap, if one is in force. Caps are keyed to the PERSON
    # rather than to a grant, so this is the only place it shows up.
    cap = db.fetch_one(
        "SELECT max_rows, expires_at, reason FROM user_row_limit_overrides "
        " WHERE slack_user_id = %s "
        "   AND (expires_at IS NULL OR expires_at > NOW()) "
        " ORDER BY max_rows DESC LIMIT 1", (slack_id,))

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
            # A NULL target is an all-targets grant: it covers every connection
            # they hold a grant on, now and later.
            "connectionId": (alias_by_id.get(r["target_server_id"])
                             or _alias_of(r["target_server_id"])),
            "allTargets": r["target_server_id"] is None,
            "tier": (r["max_tier"] or "ro").upper(),
            "databaseId": r["database_name"],
            "allDatabases": r["database_name"] is None,
            "expiresAt": mapping.iso(r["expires_at"]),
        } for r in auto],
        "admin": None if adm is None else {
            "enabled": adm["enabled"],
            # All three NULL is what makes someone a super-admin — see
            # admins.is_super_admin. Reported as a flag so the caller does not
            # have to re-derive the rule.
            "superAdmin": (adm["max_tier"] is None
                           and adm["scope_team_ids"] is None
                           and adm["scope_target_ids"] is None),
            "maxTier": (adm["max_tier"] or "").upper() or None,
            "scopeTeams": adm["scope_team_ids"],
            "scopeTargets": adm["scope_target_ids"],
            # NULL is the wildcard and an EMPTY array is "none", which is the
            # difference between an admin who can approve anything and one who
            # can approve nothing. Reading that from the shape of a value is how
            # the same distinction was got wrong once already (a scope written
            # as `{}` was treated as the wildcard), so each half gets a field of
            # its own and the client never has to tell null from [].
            "scopeTeamsAll": adm["scope_team_ids"] is None,
            "scopeTargetsAll": adm["scope_target_ids"] is None,
            "canGrant": adm["can_grant"],
        },
        "rowLimitOverride": None if cap is None else {
            "maxRows": cap["max_rows"],
            "expiresAt": mapping.iso(cap["expires_at"]),
            "reason": cap["reason"],
        },
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


# ---- Roles (access): who approves, who grants, who imports ------------------
#
# The nine-table model separates two things the `admins` table ran together:
# being an administrator, and being allowed to approve a particular request. A
# row here can say "approves for this team, up to RW, and nothing else" — the
# team lead who should see their own team's requests and no others, which the
# old scope arrays could describe but no screen could set.
#
# These write `role_assignment` DIRECTLY, unlike grants, which keep going to the
# legacy tables and reach the new model through the migration 109 mirror. That
# is safe because the mirror owns only rows it marked `mirrored_from`, and it is
# necessary because a scoped approver has no legacy row to be projected from.
#
# They take effect when `bot_config.access_model_v2` is on; until then
# `admins.can_approve` reads the old table and a row written here is inert. That
# is deliberate — the rows can be prepared and reviewed before the switch.

#: `importer` was removed in 2026-09-09: nobody held it, nothing read it, and
#: the org importer runs as the operator. A role that names an authority no
#: code checks is worse than no role — it is read as protection that is not
#: there. `granter` stayed and was WIRED instead (see grants._authz_v2).
_ROLES = ("approver", "granter", "admin")


class RoleIn(BaseModel):
    subject: str                       # the person's Slack id
    role: str
    scopeTeamId: int | None = None     # None = every team
    scopeTargetId: int | None = None   # None = every target
    maxTier: str | None = None         # None = no ceiling
    validUntil: datetime | None = None
    reason: str | None = None


def _role_row(r: dict) -> dict:
    """One role as the admin API speaks it.

    Two things are said twice on purpose. The tier goes out UPPERCASE, like
    every other tier this API serves (mapping.py), because a screen that has
    to remember which endpoint shouts and which whispers gets it wrong once
    and then shows a blank ceiling.

    And the wildcards are sent as their own booleans rather than left for the
    reader to infer from a null id. "No team" and "every team" are opposite
    answers that a missing field cannot tell apart, and this is an
    authorization screen — the difference is the whole scope.
    """
    return {"id": r["id"], "subject": r["external_id"], "name": r["display_name"],
            "role": r["role"], "enabled": bool(r["enabled"]),
            "scopeTeamId": r["scope_team_id"], "scopeTeamName": r.get("team_name"),
            "allTeams": bool(r["all_teams"]),
            "scopeTargetId": r["scope_target_id"], "scopeTargetName": r.get("alias"),
            "allTargets": bool(r["all_targets"]),
            "maxTier": (r["max_tier"] or "").upper() or None,
            "anyTier": bool(r["any_tier"]),
            "validUntil": r["valid_until"],
            "reason": r["reason"],
            # Three origins, and the screen has to tell them apart because two
            # of them cannot be revoked here: a mirrored row returns on the
            # next write to `admins`, a synced one on the next run of the sync
            # that owns it. Revoking either is a change that undoes itself.
            "source": ("mirrored" if r["mirrored_from"]
                       else "synced" if r.get("source") else "direct"),
            "syncedFrom": r.get("source")}


# `can_approve` reads the ceiling off admin and approver rows and no others, so
# storing one anywhere else puts a limit on the screen that limits nothing.
_ROLES_WITH_CEILING = ("admin", "approver")


@router.get("/roles")
def admin_roles(claims: dict = Depends(deps.current_user)):
    """Every role in force, with its scope resolved to names.

    Mirrored rows are included and marked: they are what the `admins` table
    projects into this model, and hiding them would make the screen disagree
    with what the resolver sees.

    A disabled person keeps their roles — disabling stops them submitting, it
    does not revoke anything — so `enabled` is served and the disabled are
    ordered last. A leaver still holding approval authority is the thing this
    screen exists to make visible.

    `enforced` says whether the rows are actually consulted yet. Until
    `access_model_v2` is on, `admins.can_approve` reads the old table and a
    scoped approver here decides nothing. That staging is deliberate — the
    rows are meant to be prepared and reviewed before the switch — but it was
    recorded only in a source comment, and an authorization screen that shows
    a scoped role without saying it is dormant is telling the reader something
    untrue about who can approve their requests.
    """
    admin.require_admin(claims, "review")
    rows = db.fetch_all(
        "SELECT ra.id, ra.role, ra.scope_team_id, ra.all_teams, "
        "       ra.scope_target_id, ra.all_targets, "
        "       ra.max_tier, ra.any_tier, ra.valid_until, ra.reason, "
        "       ra.mirrored_from, ra.source, i.external_id, p.display_name, "
        "       p.enabled, t.display_name AS team_name, ts.alias "
        "  FROM role_assignment ra "
        "  JOIN principal p ON p.id = ra.principal_id "
        "  JOIN principal_identity i ON i.principal_id = p.id "
        "   AND i.provider = 'slack' AND NOT i.is_deleted "
        "  LEFT JOIN team t ON t.id = ra.scope_team_id "
        "  LEFT JOIN target_servers ts ON ts.id = ra.scope_target_id "
        " WHERE NOT ra.is_deleted AND ra.revoked_at IS NULL "
        "   AND (ra.valid_until IS NULL OR ra.valid_until > NOW()) "
        " ORDER BY p.enabled DESC, p.display_name, ra.role")
    return {"roles": [_role_row(r) for r in rows],
            "enforced": teams.use_v2()}


@router.post("/roles", status_code=201)
def admin_create_role(body: RoleIn, claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    if body.role not in _ROLES:
        raise deps._error(400, "bad_request",
                          f"Unknown role. One of: {', '.join(_ROLES)}.")
    if body.role == "admin" and (body.scopeTeamId is not None
                                 or body.scopeTargetId is not None):
        # The schema refuses it too; saying so here gives a usable message
        # instead of a constraint violation.
        raise deps._error(400, "admin_scope",
                          "An admin is fleet-wide. Use approver for a scoped "
                          "role.")
    # The client speaks uppercase and the tier table is lowercase. Accept
    # either rather than making the caller know which side of the seam it is
    # on, and store the one the FK will match.
    tier = (body.maxTier or "").strip().lower() or None
    if tier is not None and tier not in ("ro", "rw", "ddl"):
        raise deps._error(400, "tier_scope", "maxTier must be RO, RW or DDL.")
    if tier is not None and body.role not in _ROLES_WITH_CEILING:
        raise deps._error(
            400, "tier_scope",
            f"A tier ceiling only applies to {' and '.join(_ROLES_WITH_CEILING)}"
            f" — nothing reads it on a {body.role}. Leave it empty.")

    with db.transaction() as cur:
        cur.execute(
            "SELECT p.id FROM principal p "
            "  JOIN principal_identity i ON i.principal_id = p.id "
            " WHERE i.provider = 'slack' AND i.external_id = %s "
            "   AND NOT i.is_deleted AND NOT p.is_deleted", (body.subject,))
        row = cur.fetchone()
        if row is None:
            raise deps._error(404, "no_account",
                              "No such person in the new access model. They "
                              "need a QueryHub account first.")
        pid = row["id"]
        if body.scopeTeamId is not None:
            cur.execute("SELECT 1 FROM team WHERE id = %s AND NOT is_deleted",
                        (body.scopeTeamId,))
            if cur.fetchone() is None:
                raise deps._error(404, "no_team", "No such team.")
        # A role is immutable here: changing one means revoking it and
        # creating the replacement, so the same statement must not be
        # writable twice. `role_assignment_live_uq` (migration 110) is the
        # real guarantee and covers the race between two of these; this
        # lookup exists to answer with a sentence naming the row rather than
        # a unique-violation traceback.
        cur.execute(
            "SELECT id, max_tier, mirrored_from FROM role_assignment "
            " WHERE principal_id = %s AND role = %s "
            "   AND scope_team_id IS NOT DISTINCT FROM %s "
            "   AND scope_target_id IS NOT DISTINCT FROM %s "
            "   AND revoked_at IS NULL AND NOT is_deleted",
            (pid, body.role, body.scopeTeamId, body.scopeTargetId))
        dup = cur.fetchone()
        if dup is not None:
            where = ("mirrors the admins table and"
                     if dup["mirrored_from"] else "already exists and")
            # `roleId` rides the envelope the way `expiredOn` rides
            # `access_expired`. The id is in the sentence too, but a client
            # that had to parse it back out of prose is how a duplicate once
            # got re-sent with `confirmed: true` — the screen's
            # revoke-and-recreate button reads this field or does not appear.
            raise deps._error(
                409, "conflict",
                f"This person already holds {body.role} over that scope "
                f"(role {dup['id']}, {where} is unchanged). Revoke it first "
                f"if you meant to change its ceiling or expiry.",
                roleId=dup["id"])
        cur.execute(
            "INSERT INTO role_assignment "
            "  (principal_id, role, scope_team_id, all_teams, scope_target_id, "
            "   all_targets, max_tier, any_tier, valid_until, reason, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "        (SELECT p.id FROM principal p "
            "           JOIN principal_identity i ON i.principal_id = p.id "
            "          WHERE i.provider = 'slack' AND i.external_id = %s "
            "            AND NOT i.is_deleted LIMIT 1)) "
            "RETURNING id",
            (pid, body.role, body.scopeTeamId, body.scopeTeamId is None,
             body.scopeTargetId, body.scopeTargetId is None, tier,
             tier is None, body.validUntil, body.reason, uid))
        rid = cur.fetchone()["id"]
        audit.log_in(cur, None, uid, claims.get("name"), "role_granted",
                     {"subject": body.subject, "role": body.role,
                      "scope_team_id": body.scopeTeamId,
                      "scope_target_id": body.scopeTargetId,
                      "max_tier": tier, "reason": body.reason})
    return {"id": rid}


@router.delete("/roles/{role_id}", status_code=204)
def admin_revoke_role(role_id: int, claims: dict = Depends(deps.current_user)):
    """Revoke, not delete: who could approve what, and until when, is a
    question an audit asks after the fact."""
    uid = admin.require_admin(claims, "access")
    with db.transaction() as cur:
        cur.execute(
            "SELECT ra.role, ra.mirrored_from, ra.source, i.external_id "
            "  FROM role_assignment ra "
            "  JOIN principal_identity i ON i.principal_id = ra.principal_id "
            "   AND i.provider = 'slack' AND NOT i.is_deleted "
            " WHERE ra.id = %s AND ra.revoked_at IS NULL AND NOT ra.is_deleted",
            (role_id,))
        row = cur.fetchone()
        if row is None:
            raise deps._error(404, "not_found", "No such active role.")
        if row["mirrored_from"]:
            # It would come straight back on the next write to that table.
            raise deps._error(
                409, "conflict",
                "This role mirrors the admins table — remove it there instead.")
        if row["source"]:
            # Same shape, different owner: the sync rebuilds its rows from the
            # team's own membership and the targets that team owns, so a
            # revoke here is undone the next time it runs. Changing who
            # approves means changing one of those two.
            raise deps._error(
                409, "conflict",
                f"This role is maintained by the '{row['source']}' sync — it "
                f"would come back on the next run. Change the team's lead or "
                f"what the team owns instead.")
        cur.execute("UPDATE role_assignment SET revoked_at = NOW(), "
                    "       revoked_by = (SELECT p.id FROM principal p "
                    "         JOIN principal_identity i ON i.principal_id = p.id "
                    "        WHERE i.provider = 'slack' AND i.external_id = %s "
                    "          AND NOT i.is_deleted LIMIT 1) "
                    " WHERE id = %s", (uid, role_id))
        audit.log_in(cur, None, uid, claims.get("name"), "role_revoked",
                     {"subject": row["external_id"], "role": row["role"],
                      "role_id": role_id})


# ---- Insights (review): audit / metrics / feedback --------------------------

# Nothing is listed here any more. The screen used an ALLOWLIST of action
# names, which meant it showed what somebody had remembered to name: 110 of
# the 132 actions this table holds were invisible, `withdrawn` among them --
# a requester withdrew a request, the admins' cards kept their buttons, and
# the audit trail did not record that anything had happened. `result_unmasked`
# was hidden too, which is a super-admin reading unmasked personal data.
#
# The same screen had already been caught hiding things once, by reading
# `audit_log_reportable` and therefore dropping the operator's own activity --
# the most privileged there is. Twice is a shape, not an accident: an audit
# view must fail towards showing too much, so the filter is now a denylist of
# per-request lifecycle noise and everything else is visible by default.
# --- masking exemptions -----------------------------------------------------
#
# The rows that switch masking OFF. Until the list below shipped they were
# invisible to anyone without psql; the write endpoints further down close the
# other half, so the ladder that makes an exemption safe to write (rung first,
# reach in a sentence, a preview of the actual data) is enforced by the product
# rather than by whoever is typing the INSERT.


def _mask_scope(r: dict) -> str:
    """Which rung of the ladder a row sits on: the narrowest field it names.

    NULL is a wildcard in this table, so the reach is decided by the narrowest
    field that is filled in and everything wider is "all of them". That is also
    why a blank form is dangerous and why the screen asks for the rung first.
    """
    if r["column_name"]:
        return "column"
    if r["table_name"]:
        return "table"
    if r["schema_name"]:
        return "schema"
    if r["database_name"]:
        return "database"
    return "server"


def _mask_covers(r: dict, target: int, database: str,
                 table: str, column: str) -> bool:
    """Does exemption row `r` already reach this exact column? NULL matches
    anything, the same way `pii._load_exemptions` reads it."""
    return (
        (r["target_server_id"] is None or r["target_server_id"] == target)
        and (r["database_name"] is None or r["database_name"] == database)
        and (r["table_name"] is None or r["table_name"] == table)
        and (r["column_name"] is None or r["column_name"] == column)
    )


@router.get("/mask-exemptions")
def admin_mask_exemptions(claims: dict = Depends(deps.current_user)):
    """Every masking exemption, what it reaches, and why the column was masked
    in the first place.

    Three things are resolved here rather than left to the screen:

    `maskedBy` is the NAME rule that caught the column, read from the same
    matcher the masker uses (`pii.explain_columns`). Half the exemptions ever
    written are for a column somebody was surprised to see masked, and the
    surprise is not knowing which rule fired. A column with no `maskedBy` is
    worth seeing too: it means the name catalog does not mask it any more, so
    the exemption is only still doing something through the value scan.

    `missing` says the exemption points at a table or column the catalog does
    not have. It is computed for the column and table rungs only, and only when
    the catalog knows that (target, database) pair at all — otherwise a target
    QueryHub has never snapshotted would report every one of its exemptions as
    dead. The wider rungs are deliberately never marked missing: "the table is
    gone" is not a state a server-wide exemption can be in.

    `alsoOn` is the suggestion the same database on another server is missing
    this exemption. A primary and its migrated copy routinely disagree, and the
    operator may have meant exactly one of them — so it is a suggestion, not an
    error, and it is only offered for a row that is switched on.
    """
    admin.require_admin(claims, "access")
    rows = db.fetch_all(
        "SELECT e.id, e.target_server_id, e.database_name, e.schema_name, "
        "       e.table_name, e.column_name, e.reason, e.enabled, "
        "       e.created_by, e.created_at, e.apply_in_joins, "
        "       e.keep_value_scan, e.super_admin_only, ts.alias "
        "  FROM pii_masking_exemptions e "
        "  LEFT JOIN target_servers ts ON ts.id = e.target_server_id "
        " ORDER BY e.enabled DESC, e.id")

    names = sorted({r["column_name"] for r in rows if r["column_name"]})
    explained = pii.explain_columns(names) if names else {}

    # One catalog read for every table any exemption names. Bounded by the
    # exemptions, not by the fleet: 457 rows against 156k catalogued columns.
    tbls = sorted({r["table_name"] for r in rows if r["table_name"]})
    dbs = sorted({r["database_name"] for r in rows if r["database_name"]})
    cat: set[tuple] = set()
    known: set[tuple] = set()
    if tbls and dbs:
        for c in db.fetch_all(
                "SELECT st.target_server_id AS t, st.database_name AS d, "
                "       st.table_name AS tb, sc.column_name AS c "
                "  FROM schema_tables st "
                "  JOIN target_servers ts ON ts.id = st.target_server_id "
                "  LEFT JOIN schema_columns sc ON sc.table_id = st.id "
                " WHERE ts.enabled AND st.table_name = ANY(%s) "
                "   AND st.database_name = ANY(%s)", (tbls, dbs)):
            known.add((c["t"], c["d"]))
            cat.add((c["t"], c["d"], c["tb"], c["c"]))
            cat.add((c["t"], c["d"], c["tb"], None))

    aliases = {r["id"]: r["alias"] for r in db.fetch_all(
        "SELECT id, alias FROM target_servers WHERE enabled")}
    live = [r for r in rows if r["enabled"]]

    out = []
    for r in rows:
        scope = _mask_scope(r)
        tid, dbname = r["target_server_id"], r["database_name"]

        missing = False
        if scope in ("column", "table") and tid is not None and dbname:
            if (tid, dbname) in known:
                missing = (tid, dbname, r["table_name"],
                           r["column_name"]) not in cat

        also = []
        if r["enabled"] and scope == "column" and dbname:
            for other, alias in sorted(aliases.items(), key=lambda kv: kv[1]):
                if other == tid:
                    continue
                if (other, dbname, r["table_name"], r["column_name"]) not in cat:
                    continue
                if any(_mask_covers(x, other, dbname, r["table_name"],
                                    r["column_name"]) for x in live):
                    continue
                also.append({"connectionId": alias, "connectionName": alias})

        out.append({
            "id": r["id"],
            "scope": scope,
            # The ALIAS, like every other connectionId in this API
            # (`_connection_entry` gives a connection `id = alias`). This
            # served the raw integer, so the one control that consumes it --
            # "Add it there", which seeds the form from a sibling server --
            # handed the form a number no connection matched, and the server
            # picker came up blank on the one path where it was prefilled.
            "connectionId": aliases.get(tid) or r["alias"],
            # NULL is a wildcard, so it is named as one. A row that reaches
            # every server must not read as a row with a blank server.
            "connectionName": r["alias"] or "every server",
            "databaseId": dbname,
            "databaseName": dbname or "every database",
            "schema": r["schema_name"],
            "table": r["table_name"],
            "column": r["column_name"],
            "strength": "soft" if r["keep_value_scan"] else "full",
            "survivesJoin": bool(r["apply_in_joins"]),
            "audience": "super" if r["super_admin_only"] else "everyone",
            "reason": r["reason"],
            "createdBy": r["created_by"],
            "createdAt": mapping.iso(r["created_at"]),
            "enabled": bool(r["enabled"]),
            "missing": missing,
            "maskedBy": explained.get(r["column_name"] or ""),
            "alsoOn": also,
        })

    return {
        "exemptions": out,
        # Masking off fleet-wide makes every row above moot, and the screen
        # says so at the top rather than implying it is doing something.
        "maskingEnabled": pii.is_enabled(),
        "nameRules": db.fetch_one(
            "SELECT count(*) AS n FROM pii_column_patterns WHERE enabled")["n"],
        "valueDetectors": [d.name for d in pii.active_detectors()],
    }


# --- masking exemptions: writes ---------------------------------------------
#
# These rows switch a protection OFF, so the write path is deliberately the
# strictest in the panel: super-admin only (`require_admin(..., "access")`),
# every call audited under a `pii_*` action so it lands in the audit trail's
# `protection` category by construction, and -- since migration 122 -- every
# change DMs the admins through the auth-event outbox, including one made from
# psql.
#
# The rung is the contract. `scope` is what the operator confirmed on screen,
# and it ALONE decides which columns of the row are written; every field
# narrower than the rung is forced to NULL rather than trusted from the body.
# Without that, a form that once had a table name in it and was widened to
# "whole server" would post a body still carrying the table, and NULL being a
# wildcard in this table means the row would be read back as a table row --
# the operator confirming one sentence and the database storing another.

_MASK_RUNGS = ("fleet", "server", "database", "schema", "table", "column")


class MaskExemptionIn(BaseModel):
    connectionId: str | None = None
    databaseId: str | None = None
    scope: str = "column"
    # `schema` shadows a BaseModel attribute in pydantic, so the field is
    # named for Python and aliased for the wire.
    schema_name: str | None = Field(None, alias="schema")
    table: str | None = None
    column: str | None = None
    strength: str = "soft"
    survivesJoin: bool = False
    audience: str = "everyone"
    reason: str = ""

    model_config = {"populate_by_name": True}


class MaskPatchIn(BaseModel):
    enabled: bool


def _mask_fields(body: MaskExemptionIn) -> tuple[int | None, dict]:
    """(target_id, row fields) for one submitted rung, or a 400.

    Fleet is read from the SCOPE, never from a missing connection. Those are
    different states -- "every server, deliberately" and "the server did not
    come through" -- and the widest row in the table must not be reachable by
    a dropdown that failed to populate.
    """
    scope = (body.scope or "").strip().lower()
    if scope not in _MASK_RUNGS:
        raise deps._error(400, "bad_request",
                          "scope must be one of: " + ", ".join(_MASK_RUNGS))

    tid = None
    if scope != "fleet":
        if not body.connectionId:
            raise deps._error(400, "bad_request", "Pick a server.")
        tid = _target_id_of(body.connectionId)
        if tid is None:
            raise deps._error(404, "not_found", "Unknown connection.")

    def _need(value: str | None, what: str) -> str:
        v = (value or "").strip()
        if not v:
            raise deps._error(400, "bad_request", f"{what} is required for the "
                                                  f"{scope} rung.")
        return v

    dbname = schema = table = column = None
    if scope in ("database", "schema", "table", "column"):
        dbname = _need(body.databaseId, "A database")
    if scope in ("schema", "table", "column"):
        schema = _need(body.schema_name, "A schema")
    elif scope == "fleet":
        # The one rung where a schema is optional: it is how the fleet-wide
        # `dba` toolkit row is written, and without it the rung means the
        # whole fleet.
        schema = (body.schema_name or "").strip() or None
    if scope in ("table", "column"):
        table = _need(body.table, "A table")
    if scope == "column":
        column = _need(body.column, "A column")

    reason = (body.reason or "").strip()
    if not reason:
        raise deps._error(400, "bad_request",
                          "A reason is required — an exemption without one is "
                          "unreadable to whoever finds it next.")

    strength = (body.strength or "soft").lower()
    if strength not in ("soft", "full"):
        raise deps._error(400, "bad_request", "strength must be soft or full.")
    audience = (body.audience or "everyone").lower()
    if audience not in ("everyone", "super"):
        raise deps._error(400, "bad_request", "audience must be everyone or super.")

    return tid, {
        "target_server_id": tid,
        "database_name": dbname,
        "schema_name": schema,
        "table_name": table,
        "column_name": column,
        # Soft keeps the value scan running; full stops both layers.
        "keep_value_scan": strength == "soft",
        "apply_in_joins": bool(body.survivesJoin),
        "super_admin_only": audience == "super",
        "reason": reason,
    }


def _mask_catalog_check(tid: int | None, dbname: str | None, schema: str | None,
                        table: str | None, column: str | None) -> None:
    """Refuse a row that names an object the catalog says is not there.

    Only when the catalog knows that (target, database) pair at all -- a target
    QueryHub has never snapshotted would otherwise refuse every exemption on
    it. Same condition the read side uses to decide whether it may call a row
    `missing`, so the screen and the write agree about when the catalog counts.
    """
    if tid is None or not dbname or not table:
        return
    known = db.fetch_one(
        "SELECT 1 AS x FROM schema_tables WHERE target_server_id = %s "
        "  AND database_name = %s LIMIT 1", (tid, dbname))
    if not known:
        return
    row = db.fetch_one(
        "SELECT st.id FROM schema_tables st "
        " WHERE st.target_server_id = %s AND st.database_name = %s "
        "   AND st.table_name = %s AND (%s::text IS NULL OR st.schema_name = %s)",
        (tid, dbname, table, schema, schema))
    if not row:
        raise deps._error(
            400, "bad_request",
            f"The catalog has no table {schema + '.' if schema else ''}{table} "
            f"in {dbname}. Pick it from the list rather than typing it.")
    if column and not db.fetch_one(
            "SELECT 1 AS x FROM schema_columns WHERE table_id = %s "
            "  AND column_name = %s", (row["id"], column)):
        raise deps._error(
            400, "bad_request",
            f"The catalog has no column {column} on {table}.")


@router.post("/mask-exemptions", status_code=201)
def admin_add_mask_exemption(body: MaskExemptionIn,
                             claims: dict = Depends(deps.current_user)):
    """Write one exemption. Super-admin only; the rung decides the row."""
    uid = admin.require_admin(claims, "access")
    tid, f = _mask_fields(body)
    _mask_catalog_check(tid, f["database_name"], f["schema_name"],
                        f["table_name"], f["column_name"])

    # A duplicate of a row that is already switched on changes nothing and makes
    # the list harder to read; a duplicate of a row that is switched OFF is
    # somebody re-adding what was deliberately retired, so it is named rather
    # than silently resurrected.
    #
    # "Duplicate" is decided by what the MASKER distinguishes, not by what the
    # columns look like. On the table and column rungs it ignores `schema_name`
    # (`pii._column_skips` matches the bare table name), so two rows differing
    # only in schema are one rule twice -- and the form makes a schema
    # mandatory on those rungs while 26 of the 30 rows written before it exist
    # without one. Comparing the column straight would have found no duplicate
    # for any of them and quietly grown a second copy of every legacy row.
    schema_matters = f["table_name"] is None
    dupe = db.fetch_one(
        "SELECT id, enabled FROM pii_masking_exemptions "
        " WHERE target_server_id IS NOT DISTINCT FROM %s "
        "   AND database_name IS NOT DISTINCT FROM %s "
        "   AND (NOT %s OR schema_name IS NOT DISTINCT FROM %s) "
        "   AND table_name IS NOT DISTINCT FROM %s "
        "   AND column_name IS NOT DISTINCT FROM %s "
        " ORDER BY enabled DESC, id LIMIT 1",
        (f["target_server_id"], f["database_name"],
         schema_matters, f["schema_name"],
         f["table_name"], f["column_name"]))
    if dupe:
        raise deps._error(
            409, "conflict",
            f"Exemption #{dupe['id']} already covers exactly this"
            + ("." if dupe["enabled"] else " — it is switched off. Turn it back "
                                           "on rather than adding a second one."))

    # NOT suppressing app.auth_dm_suppress: the migration-122 trigger is what
    # tells the other admins a protection just came off.
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO pii_masking_exemptions "
            "  (target_server_id, database_name, schema_name, table_name, "
            "   column_name, keep_value_scan, apply_in_joins, "
            "   super_admin_only, reason, created_by) "
            "VALUES (%(target_server_id)s, %(database_name)s, %(schema_name)s, "
            "        %(table_name)s, %(column_name)s, %(keep_value_scan)s, "
            "        %(apply_in_joins)s, %(super_admin_only)s, %(reason)s, %(by)s) "
            "RETURNING id", {**f, "by": uid})
        new_id = cur.fetchone()["id"]
        audit.log_in(cur, None, uid, claims.get("name"), "pii_exemption_added",
                     {"exemption_id": new_id, "scope": body.scope,
                      "target_id": tid, "database": f["database_name"],
                      "schema": f["schema_name"], "table": f["table_name"],
                      "column": f["column_name"],
                      "strength": "soft" if f["keep_value_scan"] else "full",
                      "survives_join": f["apply_in_joins"],
                      "audience": "super" if f["super_admin_only"] else "everyone",
                      "reason": f["reason"]})
    return {"id": str(new_id)}


@router.patch("/mask-exemptions/{exemption_id}")
def admin_set_mask_exemption(exemption_id: int, body: MaskPatchIn,
                             claims: dict = Depends(deps.current_user)):
    """Switch one exemption on or off. Off is the safe direction — the column
    goes back to being masked — so it is the only edit the screen offers."""
    uid = admin.require_admin(claims, "access")
    with db.transaction() as cur:
        cur.execute(
            "UPDATE pii_masking_exemptions SET enabled = %s "
            " WHERE id = %s AND enabled IS DISTINCT FROM %s "
            "RETURNING target_server_id, database_name, schema_name, "
            "          table_name, column_name",
            (body.enabled, exemption_id, body.enabled))
        row = cur.fetchone()
        if row is None:
            # Either it is gone or it is already in that state. Both are worth
            # different words: a no-op that reports success is how a screen
            # ends up showing a switch the database never moved.
            if not db.fetch_one("SELECT 1 AS x FROM pii_masking_exemptions "
                                " WHERE id = %s", (exemption_id,)):
                raise deps._error(404, "not_found", "No such exemption.")
            return {"id": str(exemption_id), "enabled": body.enabled,
                    "changed": False}
        audit.log_in(cur, None, uid, claims.get("name"), "pii_exemption_changed",
                     {"exemption_id": exemption_id, "enabled": body.enabled,
                      "target_id": row["target_server_id"],
                      "database": row["database_name"],
                      "schema": row["schema_name"], "table": row["table_name"],
                      "column": row["column_name"]})
    return {"id": str(exemption_id), "enabled": body.enabled, "changed": True}


@router.delete("/mask-exemptions/{exemption_id}", status_code=204)
def admin_delete_mask_exemption(exemption_id: int,
                                claims: dict = Depends(deps.current_user)):
    """Remove one exemption. The row's reach is copied into the audit detail
    before it goes: after the DELETE there is nothing left to say what came
    back under protection."""
    uid = admin.require_admin(claims, "access")
    with db.transaction() as cur:
        cur.execute(
            "DELETE FROM pii_masking_exemptions WHERE id = %s "
            "RETURNING target_server_id, database_name, schema_name, "
            "          table_name, column_name, enabled, reason",
            (exemption_id,))
        row = cur.fetchone()
        if row is None:
            raise deps._error(404, "not_found", "No such exemption.")
        audit.log_in(cur, None, uid, claims.get("name"), "pii_exemption_removed",
                     {"exemption_id": exemption_id,
                      "target_id": row["target_server_id"],
                      "database": row["database_name"],
                      "schema": row["schema_name"], "table": row["table_name"],
                      "column": row["column_name"],
                      "was_enabled": row["enabled"], "reason": row["reason"]})


@router.get("/mask-exemptions/catalog")
def admin_mask_catalog(connection: str | None = None,
                       database: str | None = None,
                       claims: dict = Depends(deps.current_user)):
    """Schemas -> tables -> columns for one database, each column carrying the
    name rule that masks it today.

    The form picks from this and never accepts a typed identifier, which is
    what keeps "whole server" out of reach of a typo in a table field. The rule
    on each column is the other half: an operator writing an exemption is
    almost always answering "why is this masked at all", and the answer is here
    rather than one screen away.

    Served whole, deliberately. The largest database in the fleet catalogues
    40,412 columns and that is the one page where a truncated list is worse
    than a slow one -- a table missing from the picker is a table the form will
    not let anybody exempt, with nothing on screen saying so.
    """
    admin.require_admin(claims, "access")
    # Both parameters are optional to FastAPI and required by the handler, so
    # the admin gate runs FIRST. A required query param is validated before the
    # dependency, and a 422 to an unauthenticated caller answers "this route
    # exists and takes these arguments" — which is the question the gate is
    # there not to answer.
    if not connection:
        raise deps._error(400, "bad_request", "Pick a server.")
    tid = _target_id_of(connection)
    if tid is None:
        raise deps._error(404, "not_found", "Unknown connection.")
    if not database:
        raise deps._error(400, "bad_request", "Pick a database.")

    rows = db.fetch_all(
        "SELECT st.schema_name, st.table_name, sc.column_name "
        "  FROM schema_tables st "
        "  JOIN schema_columns sc ON sc.table_id = st.id "
        " WHERE st.target_server_id = %s AND st.database_name = %s "
        " ORDER BY st.schema_name, st.table_name, sc.ordinal",
        (tid, database))
    if not rows:
        raise deps._error(
            404, "not_found",
            "No catalog snapshot for that database yet. It is written hourly; "
            "a connection added since the last run has none.")

    explained = pii.explain_columns(
        sorted({r["column_name"] for r in rows if r["column_name"]}))

    schemas: list[dict] = []
    by_schema: dict[str, dict] = {}
    by_table: dict[tuple, dict] = {}
    for r in rows:
        sname = r["schema_name"] or ""
        s = by_schema.get(sname)
        if s is None:
            s = {"name": sname, "tables": []}
            by_schema[sname] = s
            schemas.append(s)
        key = (sname, r["table_name"])
        t = by_table.get(key)
        if t is None:
            t = {"name": r["table_name"], "columns": []}
            by_table[key] = t
            s["tables"].append(t)
        t["columns"].append({"name": r["column_name"],
                             "rule": explained.get(r["column_name"])})
    return {"connectionId": connection, "databaseId": database,
            "schemas": schemas}


# --- masking exemptions: the preview ----------------------------------------
#
# This is the only place in QueryHub where a screen reads production data
# without a request behind it, and it is here because the alternative is worse:
# the sentence "unmasks `users.name`" does not tell an operator whether that
# column holds people or venues, and getting that wrong is precisely how an
# exemption removes a protection nobody meant to remove.
#
# The operator approved a real `SELECT ... LIMIT 1` against the target
# (2026-09-10). Six guardrails around it, all enforced here:
#
#   1. super-admin only, like every other write on this screen;
#   2. the RO credential, on a read-only transaction;
#   3. exactly one row, and at most six columns;
#   4. the statement is GENERATED from the catalog -- a recent user query is
#      used as EVIDENCE that the table is live, never re-executed. Re-running
#      somebody's stored SQL would run an arbitrary, arbitrarily expensive,
#      possibly writing statement from an admin screen;
#   5. a table nothing has queried recently is not read at all;
#   6. every preview is audited as `pii_exemption_preview_unmasked`, which the
#      audit vocabulary classifies protection x read -- the same category as
#      any other look at unmasked personal data.
#
# The masked/unmasked pair is computed by the REAL masker, run twice over the
# same row with the proposed exemption injected the second time. There is no
# second implementation of the ladder to drift out of step.

_MASK_PREVIEW_DAYS = 30
_MASK_PREVIEW_COLS = 6
_MASK_PREVIEW_TIMEOUT_MS = 5000


class MaskPreviewIn(BaseModel):
    connectionId: str | None = None
    databaseId: str | None = None
    scope: str = "column"
    schema_name: str | None = Field(None, alias="schema")
    table: str | None = None
    column: str | None = None
    # Not sent by the form today. Absent, the preview shows the FULL effect --
    # the wider of the two -- because a preview that understates what an
    # exemption uncovers is the one failure mode this screen cannot have.
    strength: str = "full"

    model_config = {"populate_by_name": True}


def _mask_preview_evidence(tid: int, database: str, table: str) -> dict | None:
    """The most recent real query that touched this table, or None.

    Evidence, not a script: it decides WHETHER to read the table and it is what
    the screen shows as provenance. `_tables_in` confirms the ILIKE prefilter,
    so a table whose name merely appears in a string literal does not count.
    """
    rows = db.fetch_all(
        "SELECT id, query, requester_name, requester_slack_id, engine, "
        "       COALESCE(executed_at, created_at) AS at "
        "  FROM requests "
        " WHERE target_server_id = %s AND database_name = %s "
        "   AND status = 'completed' "
        "   AND created_at >= NOW() - make_interval(days => %s) "
        "   AND query ILIKE %s "
        " ORDER BY id DESC LIMIT 50",
        (tid, database, _MASK_PREVIEW_DAYS, f"%{table}%"))
    for r in rows:
        try:
            names = pii._tables_in(r["query"], engine=r["engine"] or "postgres")
        except Exception:
            names = None
        if names and table.lower() in names:
            return r
    return None


def _mask_preview_columns(tid: int, database: str, schema: str | None,
                          table: str, column: str | None) -> list[str]:
    """Which columns the sample row shows: the one being exempted, plus enough
    neighbours to see that the rest of the row is still protected. Masked
    columns are preferred as neighbours for exactly that reason -- a diff whose
    other cells were never masked shows nothing about what stays covered."""
    rows = db.fetch_all(
        "SELECT sc.column_name AS n "
        "  FROM schema_tables st JOIN schema_columns sc ON sc.table_id = st.id "
        " WHERE st.target_server_id = %s AND st.database_name = %s "
        "   AND st.table_name = %s "
        "   AND (%s::text IS NULL OR st.schema_name = %s) "
        " ORDER BY sc.ordinal", (tid, database, table, schema, schema))
    names = [r["n"] for r in rows]
    if not names:
        return []
    explained = pii.explain_columns(names)
    picked = [column] if column and column in names else []
    for n in names:
        if len(picked) >= _MASK_PREVIEW_COLS:
            break
        if n not in picked and explained.get(n):
            picked.append(n)
    for n in names:
        if len(picked) >= _MASK_PREVIEW_COLS:
            break
        if n not in picked:
            picked.append(n)
    # Back into table order, so the row reads like the table.
    return [n for n in names if n in set(picked)]


def _mask_preview_wide(tid: int | None, database: str | None,
                       schema: str | None) -> dict:
    """The count that stands in for a sample row on the wide rungs. Reading one
    row would say nothing about a change that reaches thousands of them, and a
    single innocuous-looking row is actively misleading there."""
    where = ["TRUE"]
    args: list = []
    if tid is not None:
        where.append("st.target_server_id = %s")
        args.append(tid)
    if database:
        where.append("st.database_name = %s")
        args.append(database)
    if schema:
        where.append("st.schema_name = %s")
        args.append(schema)
    rows = db.fetch_all(
        "SELECT st.id AS tid, sc.column_name AS n "
        "  FROM schema_tables st JOIN schema_columns sc ON sc.table_id = st.id "
        " WHERE " + " AND ".join(where), tuple(args))
    if not rows:
        return {"wide": True, "tables": 0, "maskedColumns": 0}
    explained = pii.explain_columns(sorted({r["n"] for r in rows if r["n"]}))
    masked = sum(1 for r in rows if explained.get(r["n"]))
    return {"wide": True, "tables": len({r["tid"] for r in rows}),
            "maskedColumns": masked}


@router.post("/mask-exemptions/preview")
def admin_mask_preview(body: MaskPreviewIn,
                       claims: dict = Depends(deps.current_user)):
    """One real row, masked as it is today and as it would be after the row.

    The "today" side is computed for an ORDINARY reader, not for the
    super-admin running the preview: a `super_admin_only` exemption already in
    place would otherwise make the before/after look identical to the one
    person who can never see the difference.
    """
    uid = admin.require_admin(claims, "access")
    scope = (body.scope or "").strip().lower()
    if scope not in _MASK_RUNGS:
        raise deps._error(400, "bad_request", "Unknown scope.")

    tid = None
    if scope != "fleet":
        if not body.connectionId:
            raise deps._error(400, "bad_request", "Pick a server.")
        tid = _target_id_of(body.connectionId)
        if tid is None:
            raise deps._error(404, "not_found", "Unknown connection.")
    database = (body.databaseId or "").strip() or None
    schema = (body.schema_name or "").strip() or None
    table = (body.table or "").strip() or None
    column = (body.column or "").strip() or None

    if scope in ("fleet", "server", "database", "schema"):
        return _mask_preview_wide(tid, database, schema)
    if not database or not table:
        raise deps._error(400, "bad_request", "Pick a database and a table.")

    evidence = _mask_preview_evidence(tid, database, table)
    if evidence is None:
        return {"seen": False, "days": _MASK_PREVIEW_DAYS}

    target = targets.get(tid)
    if target is None or (target.engine or "postgres") != "postgres":
        # SQL Server needs TOP 1 and its own identifier quoting; ClickHouse is
        # read-only spec. Saying so beats rendering a statement that will not
        # parse on the other side.
        raise deps._error(400, "bad_request",
                          "Previews run on Postgres targets only.")

    columns = _mask_preview_columns(tid, database, schema, table, column)
    if not columns:
        raise deps._error(400, "bad_request",
                          "The catalog has no columns for that table.")

    import psycopg
    from psycopg import sql as pgsql

    tbl = (pgsql.Identifier(schema, table) if schema
           else pgsql.Identifier(table))
    stmt = pgsql.SQL("SELECT {cols} FROM {tbl} LIMIT 1").format(
        cols=pgsql.SQL(", ").join(pgsql.Identifier(c) for c in columns),
        tbl=tbl)
    try:
        db_user, password = targets.get_credentials(tid, "ro")
    except LookupError as e:
        raise deps._error(400, "bad_request", str(e))

    try:
        with psycopg.connect(
            host=target.host, port=target.port, dbname=database,
            user=db_user, password=password, connect_timeout=5,
            **cfg.target_ssl_kwargs(),
            application_name=f"queryhub:mask-preview target={target.alias}"[:63],
            options=f"-c statement_timeout={_MASK_PREVIEW_TIMEOUT_MS} "
                    f"-c idle_in_transaction_session_timeout="
                    f"{_MASK_PREVIEW_TIMEOUT_MS}",
        ) as conn, conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            rendered = stmt.as_string(conn)
            cur.execute(stmt)
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 — an unreachable target is a preview
        log.warning("mask preview failed on target %s: %s", tid, e)
        raise deps._error(502, "upstream", errors.scrub(str(e)))

    if row is None:
        # An empty table is not "nobody queried it"; it is a table with nothing
        # to show. Both leave the operator with the sentence and no sample.
        return {"seen": False, "days": _MASK_PREVIEW_DAYS, "empty": True}

    before, after = _mask_preview_pair(
        tid, database, schema, table, column, columns, list(row),
        strength=(body.strength or "full").lower())

    with db.transaction() as cur:
        audit.log_in(cur, None, uid, claims.get("name"),
                     "pii_exemption_preview_unmasked",
                     {"target_id": tid, "database": database, "schema": schema,
                      "table": table, "column": column, "scope": scope,
                      "columns": columns, "statement": rendered,
                      "evidence_request_id": evidence["id"]})

    return {
        "seen": True,
        "days": _MASK_PREVIEW_DAYS,
        # What QueryHub ran, not what the evidence query was: an audit reader
        # who sees this string must be able to trust it is the statement that
        # touched the database.
        "sql": rendered,
        "by": claims.get("name") or uid,
        "at": mapping.iso(datetime.now(timezone.utc)),
        # Provenance for the decision to read at all, kept separate so the two
        # can never be confused for one another.
        "evidence": {"requestId": str(evidence["id"]),
                     "by": evidence["requester_name"],
                     "at": mapping.iso(evidence["at"]),
                     "sql": " ".join((evidence["query"] or "").split())[:300]},
        "columns": columns,
        "before": dict(zip(columns, before)),
        "after": dict(zip(columns, after)),
    }


def _preview_cell(value) -> str:
    """One cell as the diff table shows it. NULL is named rather than rendered
    as an empty cell: "this column is empty" and "this column was blanked" are
    the two readings of a blank box, and only one of them is true."""
    if value is None:
        return "NULL"
    text = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) \
        else str(value)
    text = " ".join(text.split())
    return text if len(text) <= 120 else text[:117] + "…"


def _mask_preview_pair(tid: int, database: str, schema: str | None,
                       table: str, column: str | None, columns: list[str],
                       row: list, *, strength: str) -> tuple[list, list]:
    """The same row masked twice: as stored rules see it, and with the proposed
    exemption added. Both sides go through `pii.mask_row`, so whatever the
    executor would do to this row is what the screen shows."""
    sql = f"SELECT * FROM {schema + '.' if schema else ''}{table}"
    proposed = {
        "database_name": database, "schema_name": schema,
        "table_name": table, "column_name": column,
        "apply_in_joins": False,
        "keep_value_scan": strength == "soft",
        "super_admin_only": False,
    }
    # Strict reader on both sides: principal_id=None drops super-admin-only
    # rows, which is what almost everybody who runs a query actually gets.
    stored = pii._load_exemptions(tid, database, None)

    def _render(rows: list[dict]) -> list:
        skip_all, skip = pii.exemption_decision(
            tid, database, sql, columns, rows=rows)
        namescan = pii.exemption_namescan(
            tid, database, sql, columns, rows=rows)
        if skip_all:
            return [_preview_cell(v) for v in row]
        col_map = pii.column_pii_map(columns, sql)
        for i in skip:
            col_map.pop(i, None)
        for i in namescan:
            col_map.pop(i, None)
        masked = pii.mask_row(list(row), set(), col_map, skip_cols=skip)
        return [_preview_cell(v) for v in masked]

    return _render(stored), _render(stored + [proposed])


def _slack_ids_in(*values) -> set[str]:
    """The Slack ids among these values. A raw id is what a row without a linked
    request carries as its subject, so this is how the trail avoids showing one."""
    return {v for v in values if isinstance(v, str) and v.startswith("U")}


def _resolve_slack_names(ids) -> dict[str, str]:
    """Batched id -> display name. Shared by both audit routes: two copies of
    this lookup would be two answers to "who is this", and the screen that shows
    a bare id is the one nobody notices is wrong."""
    ids = list(ids)
    if not ids:
        return {}
    return {r["slack_user_id"]: r["name"] for r in db.fetch_all(
        "SELECT slack_user_id, name FROM requesters "
        "WHERE slack_user_id = ANY(%s)", (ids,)) if r.get("name")}


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
    params: list = [list(mapping.AUDIT_EXCLUDE)]
    where = "NOT (al.action = ANY(%s))"
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
        ids |= _slack_ids_in(d.get("user"), d.get("grantee"),
                             d.get("slack_user_id"), r.get("actor_slack_id"))
    names = _resolve_slack_names(ids)

    def _name_of(slack_id):
        return names.get(slack_id, slack_id) if slack_id else slack_id

    out = []
    for r in rows:
        k = mapping.audit_kind(r["action"])
        if kind and k != kind:
            continue
        out.append(mapping.admin_audit_entry(r, k, _alias_of, _name_of))
    return {"audit": out}


# ---------------------------------------------------------------------------
# The audit trail's one search endpoint (design 2026-09-09 (c))
# ---------------------------------------------------------------------------
#
# One endpoint answers the whole screen: the page, every denominator, the facet
# counts each chip states, and the declared exclusions with what including one
# would add. It is one endpoint because the screen's failure was disagreement
# between numbers — a chip whose size you only learn by clicking it is how the
# old Scopes bucket stayed unexamined for months.

#: The one deliberate per-request exclusion, plus usage. Both are DECLARED: the
#: screen names each, states what including it would add, and can turn it on.
#: `category` ties a slice to a kind so the chip for that kind can read
#: "excluded" instead of a contradictory 0.
AUDIT_EXCLUSIONS = (
    {"id": "lifecycle", "category": None,
     "label": "per-request lifecycle",
     "sub": "submitted, started, completed, failed — the queue and history "
            "screens show these request by request"},
    {"id": "usage", "category": "usage",
     "label": "product usage",
     "sub": "somebody opened a screen; no authority changed"},
)

_AUDIT_CATEGORIES = ("requests", "protection", "access", "connections",
                     "config", "usage", "unclassified")
_AUDIT_EFFECTS = ("added", "changed", "removed", "read", "decided", "ran")
_AUDIT_ACTOR_KINDS = ("person", "auto", "job")
_AUDIT_ACRONYMS = {"pii": "PII", "ddl": "DDL", "sql": "SQL", "ro": "RO",
                   "rw": "RW", "crm": "CRM", "dba": "DBA", "idp": "IdP",
                   "csv": "CSV"}


def _audit_label(action: str) -> str:
    """A readable label for a raw action name. Presentation only — the raw name
    is shown beside it in the rail, because the category is derived FROM it and
    an auditor questioning a classification should see the evidence."""
    words = str(action or "").replace("_", " ").split()
    if not words:
        return ""
    out = [_AUDIT_ACRONYMS.get(w.lower(), w) for w in words]
    if out[0] not in _AUDIT_ACRONYMS.values():
        out[0] = out[0][:1].upper() + out[0][1:]
    return " ".join(out)


class AuditSearchIn(BaseModel):
    q: str | None = None
    categories: list[str] | None = None
    effects: list[str] | None = None
    actorKinds: list[str] | None = None
    since: str | None = Field(None, alias="from")
    until: str | None = Field(None, alias="to")
    include: list[str] | None = None
    cursor: int | None = None
    limit: int = Field(60, ge=1, le=200)

    model_config = {"populate_by_name": True}


# Classification happens once per DISTINCT action name and is joined back, not
# called per row: 132 names against 26k rows, so the cost does not grow with the
# table. Everything else in the query hangs off this CTE.
_AUDIT_CTE = (
    "WITH names AS ("
    "  SELECT d.action, k[1] AS category, k[2] AS effect "
    "    FROM (SELECT DISTINCT action FROM audit_log) d, "
    "         LATERAL audit_classify(d.action) k), "
    "scoped AS ("
    "  SELECT al.id, al.action, n.category, n.effect, "
    "         CASE WHEN al.actor_slack_id = 'AUTO' THEN 'auto' "
    "              WHEN al.actor_slack_id IS NULL THEN 'job' "
    "              ELSE 'person' END AS actor_kind, "
    "         (al.action = ANY(%(life)s)) AS is_life "
    "    FROM audit_log al "
    "    JOIN names n ON n.action = al.action "
    "   WHERE {where}) "
)

#: The free-text search, as a UNION of ONE-TABLE branches rather than one OR
#: spanning three tables.
#:
#: The shape is the whole point. Written as a single OR across the joins, the
#: filter is applied AFTER the join, so no per-table index can serve it —
#: measured, the planner ignored four trigram indexes and sequentially scanned
#: 26k rows every time (~420 ms). Each branch here touches one table, so each
#: can be answered by a bitmap index scan and the results unioned: same rows
#: (verified identical across ten terms, including the empty and everything
#: cases), ~4x faster, and the gap widens as the table grows.
#:
#: `ILIKE '%term%'` can never use a btree — a leading wildcard has no prefix to
#: seek. The indexes are GIN over trigrams (migration 119).
_AUDIT_SEARCH_SQL = (
    "al.id IN ("
    "  SELECT a2.id FROM audit_log a2 "
    "   WHERE a2.action ILIKE %(q)s OR a2.actor_name ILIKE %(q)s "
    "      OR a2.details::text ILIKE %(q)s "
    "  UNION "
    "  SELECT a3.id FROM audit_log a3 JOIN requests r3 ON r3.id = a3.request_id "
    "   WHERE r3.requester_name ILIKE %(q)s OR r3.database_name ILIKE %(q)s "
    "      OR r3.query ILIKE %(q)s "
    "  UNION "
    "  SELECT a4.id FROM audit_log a4 JOIN requests r4 ON r4.id = a4.request_id "
    "    JOIN target_servers ts4 ON ts4.id = r4.target_server_id "
    "   WHERE ts4.alias ILIKE %(q)s)"
)


def _audit_where(body: AuditSearchIn) -> tuple[str, dict]:
    """The predicates that scope the whole screen: the search term and the date
    range. Deliberately NOT the exclusions or the chips — those are applied as
    FILTERs on one scan, so every denominator comes from the same pass."""
    where = ["TRUE"]
    params: dict = {"life": sorted(mapping.AUDIT_EXCLUDE)}
    if body.q:
        where.append(_AUDIT_SEARCH_SQL)
        params["q"] = "%" + body.q.strip() + "%"
    if body.since:
        where.append("al.created_at >= %(since)s")
        params["since"] = body.since
    if body.until:
        where.append("al.created_at <= %(until)s")
        params["until"] = body.until
    return " AND ".join(where), params


@router.post("/audit/search")
def admin_audit_search(body: AuditSearchIn,
                       claims: dict = Depends(deps.current_user)):
    """The audit trail: one page, every denominator, and the facet counts.

    Three things this endpoint is careful about, each of which is a bug the
    screen was designed to remove:

    **Category and effect are derived here, never in the client.** A client-side
    rule is a second copy of the vocabulary that drifts from the one the counts
    were computed with. `classified` rides along because a row nothing claims is
    the signal the whole screen hangs on — it is counted, not absorbed.

    **The facets are counted BEFORE the category/effect/actor filter**, so every
    chip can state the count it would produce. A filter whose size you can only
    learn by clicking it is how the old Scopes bucket stayed unexamined.

    **Four denominators, and they are different numbers.** `matched` is what the
    list shows; `total` is the scope the chips divide up; `grandTotal` adds back
    the declared exclusions; `searchTotal` is the whole trail. The screen says
    "everything the trail holds" only when nothing is excluded, and a
    completeness claim that is false by default is the exact failure this
    replaces.

    Reads `audit_log`, never `audit_log_reportable` — the reportable views drop
    the operator's own rows to keep self-test traffic out of product metrics,
    and an audit trail that omits a class of actors is not an audit trail.
    """
    admin.require_admin(claims, "review")

    bad = ([c for c in (body.categories or []) if c not in _AUDIT_CATEGORIES]
           + [e for e in (body.effects or []) if e not in _AUDIT_EFFECTS]
           + [a for a in (body.actorKinds or []) if a not in _AUDIT_ACTOR_KINDS])
    if bad:
        raise deps._error(400, "bad_request",
                          f"Unknown filter value: {', '.join(sorted(bad))}.")

    where, params = _audit_where(body)
    cte = _AUDIT_CTE.format(where=where)
    include = set(body.include or [])
    show_life = "lifecycle" in include
    show_usage = "usage" in include

    # ONE scan, grouped by the few things every number is derived from. The grid
    # is at most (action x actor kind x lifecycle) rows — a couple of hundred —
    # so the page below is the only other pass over the table.
    grid = db.fetch_all(
        cte + "SELECT action, category, effect, actor_kind, is_life, "
              "       count(*) AS n FROM scoped "
              " GROUP BY action, category, effect, actor_kind, is_life", params)

    def in_scope(g) -> bool:
        if g["is_life"] and not show_life:
            return False
        if g["category"] == "usage" and not show_usage:
            return False
        return True

    def chips_ok(g) -> bool:
        if body.categories and g["category"] not in body.categories:
            return False
        if body.effects and g["effect"] not in body.effects:
            return False
        if body.actorKinds and g["actor_kind"] not in body.actorKinds:
            return False
        return True

    facets_c = {k: 0 for k in _AUDIT_CATEGORIES}
    facets_e = {k: 0 for k in _AUDIT_EFFECTS}
    facets_a = {k: 0 for k in _AUDIT_ACTOR_KINDS}
    grand_total = matched = total = 0
    hidden = {x["id"]: 0 for x in AUDIT_EXCLUSIONS}
    types: set[str] = set()
    types_unclassified: set[str] = set()
    for g in grid:
        n = g["n"]
        grand_total += n
        if g["is_life"] and not show_life:
            hidden["lifecycle"] += n
        elif g["category"] == "usage" and not show_usage:
            # `elif`: a lifecycle row is counted once, under the slice that is
            # actually hiding it, or the two numbers would double-count and the
            # control would promise rows the other one already promised.
            hidden["usage"] += n
        if not in_scope(g):
            continue
        total += n
        facets_c[g["category"]] = facets_c.get(g["category"], 0) + n
        facets_e[g["effect"]] = facets_e.get(g["effect"], 0) + n
        facets_a[g["actor_kind"]] = facets_a.get(g["actor_kind"], 0) + n
        types.add(g["action"])
        if g["category"] == "unclassified":
            types_unclassified.add(g["action"])
        if chips_ok(g):
            matched += n

    # The page. Same CTE, the chips applied, keyset on id so a cursor cannot
    # skip or repeat a row when new ones arrive mid-read.
    page_where = ["TRUE"]
    pp = dict(params)
    if not show_life:
        page_where.append("NOT s.is_life")
    if not show_usage:
        page_where.append("s.category <> 'usage'")
    if body.categories:
        page_where.append("s.category = ANY(%(cats)s)")
        pp["cats"] = list(body.categories)
    if body.effects:
        page_where.append("s.effect = ANY(%(effs)s)")
        pp["effs"] = list(body.effects)
    if body.actorKinds:
        page_where.append("s.actor_kind = ANY(%(acts)s)")
        pp["acts"] = list(body.actorKinds)
    if body.cursor:
        page_where.append("s.id < %(cursor)s")
        pp["cursor"] = body.cursor
    pp["lim"] = body.limit

    rows = db.fetch_all(
        cte + "SELECT s.id, s.category, s.effect, s.actor_kind, "
              "  al.action, al.actor_slack_id, al.actor_name, al.details, "
              "  al.created_at, al.request_id, "
              "  r.requester_name, r.database_name, r.query, r.row_count, "
              "  r.required_tier, r.executed_at, r.completed_at, "
              "  ts.alias AS target_alias, "
              "  g.slack_user_id AS via_user, g.max_tier AS via_tier, "
              "  g.database_name AS via_db, gts.alias AS via_target "
              "  FROM scoped s "
              "  JOIN audit_log al ON al.id = s.id "
              "  LEFT JOIN requests r ON r.id = al.request_id "
              "  LEFT JOIN target_servers ts ON ts.id = r.target_server_id "
              # The authority a machine acted under. Recorded at write time as a
              # REFERENCE (`grant_id`), so naming it is a join — and the window
              # can be gone, which is why the null has to be distinguishable
              # from "there was never one".
              "  LEFT JOIN auto_approve_grants g "
              "    ON s.actor_kind = 'auto' "
              "   AND al.details ->> 'grant_id' ~ '^[0-9]+$' "
              "   AND g.id = (al.details ->> 'grant_id')::int "
              "  LEFT JOIN target_servers gts ON gts.id = g.target_server_id "
              f" WHERE {' AND '.join(page_where)} "
              " ORDER BY s.id DESC LIMIT %(lim)s", pp)

    ids = set()
    for r in rows:
        d = r.get("details") if isinstance(r.get("details"), dict) else {}
        ids |= _slack_ids_in(d.get("user"), d.get("grantee"),
                             d.get("slack_user_id"), r.get("actor_slack_id"),
                             r.get("via_user"))
    names = _resolve_slack_names(ids)

    out = [_audit_row(r, names) for r in rows]
    cursor = out[-1]["id"] if len(rows) == body.limit else None
    search_total = db.fetch_one("SELECT count(*) AS n FROM audit_log")["n"]

    return {
        "rows": out,
        "cursor": cursor,
        "matched": matched,
        "total": total,
        "grandTotal": grand_total,
        "searchTotal": search_total,
        "facets": {"categories": facets_c, "effects": facets_e,
                   "actorKinds": facets_a},
        "actionTypes": {"total": len(types),
                        "unclassified": len(types_unclassified)},
        "exclusions": [
            {**{k: x[k] for k in ("id", "label", "sub", "category")},
             "included": x["id"] in include,
             # What INCLUDING it would add. Zero once it is included — the
             # control then reads "shown" rather than promising rows twice.
             "rows": hidden[x["id"]]}
            for x in AUDIT_EXCLUSIONS
        ],
    }


def _audit_via(r: dict, names: dict) -> str | None:
    """How a machine came to act. Three outcomes, and the screen renders each
    differently, so they must stay distinguishable:

    * a window that still exists — named, subject to target at a tier;
    * a window whose id is on the row but which has since been deleted —
      `auto_approve_grants` has no `revoked_at`, rows are removed, and 2 of the
      4,477 auto-approvals on this trail point at one that is gone;
    * a fingerprint match, which names the earlier request instead.

    None means the row is not a machine action at all.
    """
    d = r.get("details") if isinstance(r.get("details"), dict) else {}
    fp = d.get("fingerprint_match_request_id")
    if fp:
        return f"query fingerprint matched request #{fp}"
    if not d.get("grant_id"):
        return None
    if not r.get("via_user"):
        return "an auto-approve window that no longer exists"
    who = names.get(r["via_user"], r["via_user"])
    target = r.get("via_target") or "any target"
    if r.get("via_db"):
        target += "/" + r["via_db"]
    tier = (r.get("via_tier") or "").upper()
    return f"auto-approve window · {who} → {target}{(' ' + tier) if tier else ''}"


def _audit_row(r: dict, names: dict) -> dict:
    """One row, in the shape the screen reads. `actor.name` stays null when no
    named account exists — never a fabricated display name, and never blank."""
    handle = r.get("actor_slack_id") or "system"
    name = r.get("actor_name") or names.get(r.get("actor_slack_id") or "")
    req = None
    if r.get("request_id"):
        dur = None
        if r.get("executed_at") and r.get("completed_at"):
            dur = int((r["completed_at"] - r["executed_at"]).total_seconds() * 1000)
        req = {
            "id": r["request_id"],
            "requester": r.get("requester_name") or "—",
            "connection": r.get("target_alias") or "—",
            "database": r.get("database_name") or "—",
            "tier": (r.get("required_tier") or "").upper() or None,
            "rows": r.get("row_count"),
            "durationMs": dur,
            "sql": r.get("query"),
        }
    target = None
    if r.get("target_alias"):
        target = r["target_alias"] + ("/" + r["database_name"]
                                      if r.get("database_name") else "")
    return {
        "id": r["id"],
        "time": mapping.iso(r["created_at"]),
        "actor": {"handle": handle, "name": name or None,
                  "kind": r["actor_kind"], "via": _audit_via(r, names)},
        "action": r["action"],
        "actionLabel": _audit_label(r["action"]),
        "category": r["category"],
        "effect": r["effect"],
        "classified": r["category"] != "unclassified",
        "target": target,
        "detail": r.get("details") if isinstance(r.get("details"), dict) else {},
        "request": req,
    }


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


# ---------------------------------------------------------------------------
# Layer-A reconcile, driven by the IDP panel (PLA-479)
# ---------------------------------------------------------------------------

class PrincipalSyncIn(BaseModel):
    """The FULL desired state, as verified corporate addresses.

    Not a delta: a delta protocol drifts silently the first time a message is
    lost, and nothing ever notices. A reconcile compares the whole set every
    run, so a missed push self-heals on the next one.
    """
    emails: list[str] = Field(default_factory=list)
    admin_emails: list[str] = Field(default_factory=list)


@router.post("/principals/sync")
def principals_sync(body: PrincipalSyncIn,
                    claims: dict = Depends(deps.current_user)):
    """Apply the panel's view of who may use QueryHub.

    Gated twice. `require_admin` is the structural gate every /api/admin route
    carries; on top of it the caller must be the exact principal named in
    `bot_config.idp_sync_principal`. 'review' rather than 'access' is
    deliberate: the sync account needs to be *an* admin, and making a machine
    account a super-admin — able to approve anything if its key leaked — would
    be a real downgrade paid to satisfy a gate. Scope its admin row to nothing
    (`scope_team_ids='{}'`, `scope_target_ids='{}'`) and it can approve no
    request at all while still passing here.

    Enable and disable requesters only. Onboarding needs a Slack id the panel
    does not own, so an address with no row comes back in `unresolved` for a
    human to act on.

    It does not write to the `admins` table at all, in either direction.
    Promotion was never implemented; disabling was, and the empty-state guard
    covered only requesters, so a caller sending a populated `emails` with an
    empty `admin_emails` disabled EVERY admin and stopped approvals dead. Admin
    membership is now a human decision made in QueryHub, which puts it outside
    a compromised panel's reach entirely. What the panel believes is reported
    as `admin_drift` for an operator to act on, and every reconcile writes an
    audit row: a permission change nobody can reconstruct afterwards is a
    permission change nobody can review.
    """
    uid = admin.require_admin(claims, "review")
    expected = (cfg.get_setting("idp_sync_principal", "") or "").strip()
    if not expected or uid != expected:
        raise deps._error(403, "forbidden", "Not the sync principal.")

    live = requesters.list_enabled_ids()
    if live and not body.emails:
        raise deps._error(
            400, "empty_sync_refused",
            "Refusing a sync that would disable every requester.")

    unresolved: list[str] = []
    want: set[str] = set()
    for email in body.emails:
        row = requesters.by_email(email) or admins.by_email(email)
        if row is None:
            unresolved.append(email)
            continue
        want.add(row["slack_user_id"])

    enabled_ids = sorted(want - live)
    for pid in enabled_ids:
        requesters.enable(pid)
    disabled_ids = sorted(live - want)
    for pid in disabled_ids:
        requesters.disable(pid)

    # Report-only. Resolving the panel's admin list costs one lookup each and
    # tells an operator exactly what to reconcile by hand.
    want_admins: set[str] = set()
    for email in body.admin_emails:
        row = admins.by_email(email) or requesters.by_email(email)
        if row is None:
            if email not in unresolved:
                unresolved.append(email)
            continue
        want_admins.add(row["slack_user_id"])
    live_admins = {a["slack_user_id"] for a in admins.list_active()
                   if a.get("source") == "permanent"}
    drift = {"not_admin_here": sorted(want_admins - live_admins),
             "not_listed_by_panel": sorted(live_admins - want_admins)}

    audit.log(None, uid, "idp-sync", "idp_principal_sync",
              {"enabled": enabled_ids, "disabled": disabled_ids,
               "unresolved": unresolved, "admin_drift": drift})

    log.info("layer-A reconcile: +%d/-%d requesters, %d unresolved, drift %s",
             len(enabled_ids), len(disabled_ids), len(unresolved), drift)
    return {"requesters": {"enabled": len(enabled_ids),
                           "disabled": len(disabled_ids)},
            "admin_drift": drift,
            "unresolved": unresolved}


# ---------------------------------------------------------------------------
# Notification outbox (PLA-479 task A2) — the panel's poll target
# ---------------------------------------------------------------------------
#
# The IDP panel has no admins table of its own and must not grow one, so
# QueryHub decides who should be told a request is waiting (see
# migrations/102_notification_outbox.sql, written by
# core_submit.dispatch_and_notify) and the panel polls this pair of routes:
# GET lists what's pending, POST stamps one row done.
#
# GET does NOT claim rows — no FOR UPDATE SKIP LOCKED, unlike
# auth_events.py's own poller. A claim here would need a lease this endpoint
# has no way to release: the panel stamps processed_at in a SEPARATE request
# that may simply never arrive (the poller crashes, delivery is dropped), and
# a plain repeatable list is the only shape under which polling again after a
# lost stamp re-lists the same row instead of leaving it stuck behind a
# claim nobody will release.
#
# POST .../processed is a single UPDATE guarded by `processed_at IS NULL`,
# with no preceding existence check. That is what makes it idempotent by
# construction rather than by a special case: the panel's poller is
# at-least-once (it can crash between delivering and stamping), so a second
# POST for an already-stamped row — or even a never-existing id — updates
# zero rows and still answers 204, never an error.

_OUTBOX_DEFAULT_LIMIT = 50
_OUTBOX_MAX_LIMIT = 500


def _resolve_recipient_emails(slack_ids: list[str]) -> dict[str, str]:
    """slack_user_id -> work email, resolved at READ time (Ruling P-1).

    `notification_outbox.recipients` stores whatever `admins.list_active()`
    yielded at write time — mostly permanent `admins` rows, but a temp-admin
    grantee's own row lives in `requesters` instead (see `list_active`'s
    COALESCE across the two tables). Checking admins first, then requesters
    for whatever is still missing, covers both without assuming a person
    exists in only one. Resolving here rather than caching at write time
    means an admin whose email changes while a row sits unprocessed is
    notified at the CURRENT address. A slack_user_id resolving in neither
    table is simply absent from the result — the caller counts what it
    could not resolve.
    """
    ids = sorted({s for s in slack_ids if s})
    if not ids:
        return {}
    resolved: dict[str, str] = {}
    for row in db.fetch_all(
            "SELECT slack_user_id, email FROM admins "
            "WHERE slack_user_id = ANY(%s) AND email IS NOT NULL "
            "AND btrim(email) <> ''", (ids,)):
        resolved[row["slack_user_id"]] = row["email"]
    missing = [s for s in ids if s not in resolved]
    if missing:
        for row in db.fetch_all(
                "SELECT slack_user_id, email FROM requesters "
                "WHERE slack_user_id = ANY(%s) AND email IS NOT NULL "
                "AND btrim(email) <> ''", (missing,)):
            resolved[row["slack_user_id"]] = row["email"]
    return resolved


def _stringify_payload(payload) -> dict[str, str]:
    """Every payload value served as a string — Ruling P-10.

    The panel decodes this into `map[string]string` (its templated-message
    Vars are all strings downstream), and Go's `json.Unmarshal` refuses a
    JSON NUMBER into a string field for the WHOLE response, not just the
    one offending key — one non-string value anywhere in a batch silently
    stops every pending notification in it from being delivered. A1's own
    writer stores `"requestId": row["id"]`, a Python int, which is exactly
    this shape (core_submit.py). A future writer in this repo cannot see
    the panel's Go-side contract to know not to repeat it, so this coerces
    at the one place that serves the wire shape, rather than trusting every
    writer, present and future, to remember.
    """
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in payload.items():
        if isinstance(v, str):
            out[k] = v
        elif isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif v is None:
            out[k] = ""
        elif isinstance(v, (int, float)):
            out[k] = str(v)
        else:
            out[k] = json.dumps(v)
    return out


@router.get("/notifications/outbox")
def notifications_outbox(limit: int = _OUTBOX_DEFAULT_LIMIT,
                         claims: dict = Depends(deps.current_user)):
    """Unprocessed notification_outbox rows, oldest first — see the section
    comment above for why this lists rather than claims. `recipients` are
    served as work emails (Ruling P-1); `unresolvedRecipients` counts
    recipients this call could not resolve to an email, so the panel can
    attribute a drop to this side of the seam instead of guessing."""
    admin.require_admin(claims, "review")
    lim = limit if 1 <= limit <= _OUTBOX_MAX_LIMIT else _OUTBOX_DEFAULT_LIMIT
    rows = db.fetch_all(
        "SELECT id, event_type, request_id, recipients, payload, created_at "
        "  FROM notification_outbox "
        " WHERE processed_at IS NULL "
        " ORDER BY created_at, id "
        " LIMIT %s", (lim,))
    all_ids = {sid for r in rows for sid in (r["recipients"] or [])}
    emails = _resolve_recipient_emails(list(all_ids))
    events = []
    unresolved = 0
    for r in rows:
        recipient_emails = []
        for sid in (r["recipients"] or []):
            email = emails.get(sid)
            if email:
                recipient_emails.append(email)
            else:
                unresolved += 1
        events.append({
            "id": r["id"],
            "eventType": r["event_type"],
            "requestId": r["request_id"],
            "recipients": recipient_emails,
            "payload": _stringify_payload(r["payload"]),
            "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return {"events": events, "unresolvedRecipients": unresolved}


@router.post("/notifications/outbox/{outbox_id}/processed", status_code=204)
def notifications_outbox_processed(outbox_id: int,
                                   claims: dict = Depends(deps.current_user)):
    """Stamp one outbox row processed. Idempotent by construction — see the
    section comment above: the UPDATE's own guard means a second call for an
    already-stamped (or never-existing) id touches zero rows and still
    answers 204."""
    admin.require_admin(claims, "review")
    db.execute(
        "UPDATE notification_outbox SET processed_at = NOW() "
        " WHERE id = %s AND processed_at IS NULL", (outbox_id,))
