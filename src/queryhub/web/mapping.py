"""Pure mapping helpers: core rows → API_CONTRACT JSON shapes.

Kept side-effect-free so the contract-critical conversions (status
strings, history rows, env labels) are unit-testable without a DB.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import config as cfg
from .. import query_safety
from ..auto_approve import AUTO_DECIDED_BY

log = logging.getLogger(__name__)

# Timezone the web UI renders timestamps in. bot_config
# `web_display_timezone` (an IANA name like "Europe/Istanbul"); defaults to
# UTC so a fresh install shows unambiguous times. Stored datetimes are UTC.
_TZ_CACHE: dict[str, ZoneInfo] = {}


def _display_tz() -> ZoneInfo:
    name = (cfg.get_setting("web_display_timezone", "UTC") or "UTC").strip() or "UTC"
    tz = _TZ_CACHE.get(name)
    if tz is None:
        try:
            tz = ZoneInfo(name)
        except Exception:
            tz = ZoneInfo("UTC")
        _TZ_CACHE[name] = tz
    return tz


def _to_display(dt):
    """A UTC (or naive-UTC) datetime moved into the display timezone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_display_tz())


def fmt_ts_ms(dt) -> str:
    """yyyy-MM-dd HH:mm:ss.fff in the display timezone (millisecond precision)."""
    d = _to_display(dt)
    return f"{d:%Y-%m-%d %H:%M:%S}.{d.microsecond // 1000:03d}" if d else ""


def fmt_time(dt) -> str:
    """HH:mm:ss in the display timezone."""
    d = _to_display(dt)
    return f"{d:%H:%M:%S}" if d else ""

# requests.status → the exact strings the frontend renders
# (pending | approved | running | done | rejected | failed).
_STATUS_TO_WEB = {
    "pending": "pending",
    "approved": "approved",
    "scheduled": "approved",            # approved, waiting for its run time
    "executing": "running",
    "awaiting_dba_manual": "running",   # in DBA hands; still in flight
    "completed": "done",
    "failed": "failed",
    "rejected": "rejected",
    "changes_requested": "rejected",    # terminal for this submission; note says why
    "cancelled": "rejected",
    "expired": "rejected",
}


def status_to_web(status: str) -> str:
    return _STATUS_TO_WEB.get(status, "pending")


def env_of(alias: str) -> str:
    """production | staging — drives the env dot in the UI. The fleet is
    prod-first; anything that self-describes as test/staging is staging."""
    a = (alias or "").lower()
    if "test" in a or "staging" in a or "stage" in a:
        return "staging"
    return "production"


_ENGINE_LABELS = {"postgres": "PostgreSQL", "clickhouse": "ClickHouse",
                  "sqlserver": "SQL Server", "mysql": "MySQL"}


def engine_label(engine: str | None) -> str:
    return _ENGINE_LABELS.get((engine or "").lower(), engine or "PostgreSQL")


def approver_label(decided_by_slack_id: str | None,
                   decided_by_name: str | None) -> str | None:
    if decided_by_slack_id == AUTO_DECIDED_BY:
        return "auto-approve"
    return decided_by_name


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


_SLACK_SHORTCODE = re.compile(r":[a-z0-9_'+-]+:")
_SLACK_LINK = re.compile(r"<([^|>]+)\|([^>]+)>")


def web_text(s: str | None) -> str | None:
    """Strip Slack mrkdwn artifacts (emoji shortcodes, backticks, <url|text>
    links) from a backend string so it renders cleanly in the web UI — the
    web contract must never carry Slack-only formatting. risk_summary and
    other Slack-flavored fields pass through here."""
    if not s:
        return s
    s = _SLACK_LINK.sub(r"\2", s)
    s = _SLACK_SHORTCODE.sub("", s)
    s = s.replace("`", "")
    return " ".join(s.split()).strip()


def history_entry(row: dict, alias_of: "callable") -> dict:
    """One requests row → the /history shape."""
    return {
        "id": str(row["id"]),
        "sql": row["query"],
        "connectionId": alias_of(row["target_server_id"]) or str(row["target_server_id"]),
        "databaseId": row["database_name"],
        "tier": query_safety.required_mode(row["query"]).upper(),
        "status": status_to_web(row["status"]),
        "rowCount": row.get("row_count"),
        "createdAt": iso(row.get("created_at")),
        "approver": approver_label(row.get("decided_by_slack_id"),
                                   row.get("decided_by_name")),
    }


def saved_entry(row: dict, alias_of: "callable") -> dict:
    """One query_favorites row → the /saved shape."""
    tid = row.get("target_server_id")
    return {
        "id": str(row["id"]),
        "name": row.get("label") or (row["query"][:40] + ("…" if len(row["query"]) > 40 else "")),
        "connectionId": (alias_of(tid) if tid else None),
        "databaseId": row.get("database_name"),
        "sql": row["query"],
    }


def session_entry(row: dict) -> dict:
    """One web_saved_sessions row → the /sessions shape (always dest=server;
    local sessions never reach the server)."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "dest": "server",
        "savedAt": iso(row.get("updated_at")),
        "tabs": [{"name": t.get("name"), "sql": t.get("sql") or "",
                  "conn": t.get("connectionId"), "db": t.get("databaseId")}
                 for t in (row.get("tabs") or [])],
    }


def scheduled_entry(row: dict, alias_of: "callable") -> dict:
    """A scheduled `requests` row → the /scheduled shape. The real scheduled
    queries live in `requests` (status='scheduled'), so the panel shows the
    genuine pending runs — not a disconnected client list."""
    sql = row["query"] or ""
    tid = row.get("target_server_id")
    first = sql.strip().splitlines()[0].strip() if sql.strip() else ""
    return {
        "id": str(row["id"]),
        "name": (first[:40] or "Scheduled query"),
        "conn": (alias_of(tid) if tid else None),
        "db": row.get("database_name"),
        "sql": sql,
        "when": iso(row.get("scheduled_for")),
        "createdAt": iso(row.get("created_at")),
    }


# ---- GET /admin/queue -------------------------------------------------------

def _initials(name: str | None) -> str:
    parts = [p for p in (name or "").replace(".", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def queue_item(row: dict, alias_of: "callable", tags_of=None) -> dict:
    """One pending `requests` row → the ADMIN_API GET /admin/queue item.

    `trust` has no backend model yet (None); `estRows`/`estTables` aren't
    persisted as discrete fields — the render-ready `risk_summary` we do
    store is surfaced as `riskSummary` instead.

    `tags_of` resolves the target's hosting bag SERVER-side. The client must
    not join it: an approver saying yes to a DDL is being shown where it runs,
    and a client-side join is a stale cloud name next to a DROP.
    """
    tid = row.get("target_server_id")
    alias = alias_of(tid) if tid else None
    query = row.get("query") or ""
    tier = query_safety.required_mode(query).upper()
    name = row.get("requester_name")
    return {
        "id": str(row["id"]),
        "submitter": {
            "name": name or row.get("requester_slack_id"),
            "initials": _initials(name),
            "slackId": row.get("requester_slack_id"),
            "trust": None,
        },
        "connectionId": alias or (str(tid) if tid else None),
        "databaseId": row.get("database_name"),
        "tags": (tags_of(tid) if (tags_of and tid) else {}) or {},
        "env": env_of(alias or ""),
        "tier": tier,
        "sql": query,
        "statements": len(classification_of(query)["statements"]),
        "piiCols": pii_preview(query)["columns"],
        "estRows": None,
        "estTables": [],
        # Same field, two names, and only one of them is honest. The write side
        # takes `justification` (POST /queries); this mapper invented `reason`
        # because it was written separately, so the approver's UI reads
        # `it.reason` for a value the requester submitted as `justification`.
        # Both are emitted while the frontend moves over; ADMIN_API.md documents
        # `reason` as the deprecated alias.
        "justification": row.get("justification"),
        "reason": row.get("justification"),
        "riskSummary": web_text(row.get("risk_summary")),
        "submittedAt": iso(row.get("created_at")),
        "escalate": tier == "DDL",
        "bundleId": row.get("bundle_id"),
        "origin": (row.get("origin") or "slack"),
    }


# ---- Access control (super-admin) read shapes -------------------------------

def grant_entry(row: dict, alias_of: "callable") -> dict:
    """A user_target_grants / team_target_grants row → admin grant shape.
    The row must carry the synthesized keys `_gid` (stable id) and
    `_subject_type` ('user'|'team'), plus `subject`/`subject_name`.
    `databases` is the allowed_databases list, or "*" for unrestricted —
    grants are per-target with a db list, so there is no single databaseId.
    """
    dbs = row.get("allowed_databases")
    return {
        "id": row["_gid"],
        "subjectType": row["_subject_type"],
        "subject": row.get("subject"),
        "subjectName": row.get("subject_name"),
        "connectionId": alias_of(row.get("target_server_id"))
        or (str(row["target_server_id"]) if row.get("target_server_id") else None),
        "databases": sorted(dbs) if dbs else "*",
        "tier": (row.get("mode") or "ro").upper(),
        "grantedBy": row.get("granted_by"),
        "grantedAt": iso(row.get("granted_at")),
        "expiresAt": None,          # neither grant table stores an expiry
    }


def auto_grant_entry(row: dict, alias_of: "callable") -> dict:
    """One auto_approve_grants row → admin auto-grant shape. `maxRows` has
    no per-grant model (row caps live in user_row_limit_overrides), so it
    is null here."""
    return {
        "id": str(row["id"]),
        "user": row.get("slack_user_id"),
        "tier": (row.get("max_tier") or "ro").upper(),
        "connectionId": alias_of(row.get("target_server_id"))
        or (str(row["target_server_id"]) if row.get("target_server_id") else None),
        "databaseId": row.get("database_name"),
        "maxRows": None,
        "reason": row.get("reason"),
        "expiresAt": iso(row.get("expires_at")),
        "createdBy": row.get("granted_by"),
        "grantedAt": iso(row.get("granted_at")),
    }


# ---- Insights (audit / feedback) -------------------------------------------

# audit_log.action -> the contract's audit kind (approve|reject|changes|grant|
# auto|scope|kill). Only these actions surface in the admin audit trail — so a
# missing key means an entire class of activity is invisible there, which is the
# opposite of what an audit trail is for.
#
# `auto_approved_super` was missing, and it is the action that approves a
# SUPER-ADMIN's own query. The effect: everyone else's executions appeared in the
# audit view and the most privileged user's did not. Reported as "my queries do
# not show up in the audit log"; the rows were in `audit_log` all along, filtered
# out by this allow-list.
AUDIT_KIND = {
    "approved": "approve", "auto_approved": "approve",
    "auto_approved_fingerprint": "approve",
    "auto_approved_super": "approve",
    "auto_approve_window_approved": "approve", "import_approved": "approve",
    "completed_manually": "approve",
    "rejected": "reject", "cancelled": "reject", "escalated_to_dba": "reject",
    "changes_requested": "changes",
    "user_grant_added": "grant", "team_grant_added": "grant",
    "team_grant_removed": "grant", "access_revoked": "grant",
    "user_grant_revoked": "grant", "user_grant_removed": "grant",
    "endpoint_provisioned": "grant", "endpoint_rejected": "reject",
    "access_request_auto_grant": "grant",
    "pii_exemption_added": "grant", "access_granted": "grant",
    "auto_approve_grant_created": "auto", "auto_approve_revoked": "auto",
    "admin_scope_set": "scope", "admin_scope_removed": "scope",
    "team_created": "scope", "team_updated": "scope",
    "team_deleted": "scope", "person_teams_set": "scope",
    "web_login": "access", "web_signout": "access",
    "slack_sql_opened": "access",
    "kill_switch_set": "kill",
}

# Friendlier event labels for actions whose raw name reads poorly. Anything not
# here falls back to the underscore→space form in admin_audit_entry.
_EVENT_LABEL = {
    "web_login": "Signed in to web",
    "web_signout": "Signed out of web",
    "slack_sql_opened": "Opened /sql in Slack",
    "user_grant_added": "User grant added",
    "user_grant_revoked": "User grant revoked",
    "user_grant_removed": "User grant revoked",
    "access_revoked": "Access revoked",
    "team_grant_added": "Team grant added",
    "team_grant_removed": "Team grant removed",
}


# How a sign-in identifies itself, in words that do not collide with the
# "via web" / "via Slack" the request rows use for the SURFACE a query arrived
# from. Two vocabularies, deliberately kept apart:
#
#     provider  →  what proved who you are      Slack SSO / local account
#     origin    →  where the request came from   via web / via Slack
#
# One key per BUILT-IN provider (auth_providers._ALL) — a test fails if one is
# added without a label here, because the fallback below is a safety net and
# not a plan.
#
# Operator-configured OIDC providers cannot be in this map: their ids are
# chosen per deployment and only the environment knows them. They are named
# from the live registry instead, which is why `_provider_label` is a function.
#
# The fallback exists for a row written by an older or newer build than the one
# rendering it, or by a provider since removed from the environment: audit rows
# are permanent and the provider set is not. It shows "<name> sign-in" rather
# than hiding the line, because a login nobody can attribute is precisely what
# an audit reader needs to see.
_PROVIDER_LABELS = {
    "slack": "Slack SSO",
    "local": "local account",
}


def _provider_label(provider: str) -> str:
    name = str(provider).lower()
    fixed = _PROVIDER_LABELS.get(name)
    if fixed:
        return fixed
    from . import auth_providers
    try:
        if name in auth_providers.oidc_ids():
            return f"{name} SSO"
    except Exception:          # never let a label lookup break an audit page
        log.warning("provider label lookup failed for %r", name, exc_info=True)
    return f"{provider} sign-in"


def _audit_info(d: dict) -> str | None:
    """A general, human-readable detail line for an audit row, built from the
    parts of `details` that the fixed columns (actor/event/target/tier) don't
    already show — e.g. sign-in context (provider/IP/user-agent), grant scope
    (reason/expiry/allowed dbs), notes. Returns None when there's nothing extra."""
    if not isinstance(d, dict) or not d:
        return None
    bits: list[str] = []
    if d.get("provider"):
        # Name the identity provider, and do NOT say "via".
        #
        # "via" is already taken in this same list: a submitted request renders
        # "via web" or "via Slack" meaning the SURFACE it arrived from. A sign-in
        # row carries `provider`, which is the thing that proved who you are —
        # so "Signed in to web · via slack" read as a contradiction and was
        # reported as a bug. It was not: someone signed into the web app using
        # Slack SSO, which is both true and the whole point of the provider
        # registry. One word, two meanings, one list.
        bits.append(_provider_label(d["provider"]))
    if d.get("ip"):
        bits.append(f"IP {d['ip']}")
    ua = d.get("user_agent")
    if ua:
        ua = str(ua)
        bits.append(ua if len(ua) <= 72 else ua[:71] + "…")
    ad = d.get("allowed_databases")
    if isinstance(ad, list) and ad:
        bits.append("dbs: " + ", ".join(str(x) for x in ad))
    if d.get("expires_at"):
        bits.append(f"expires {str(d['expires_at'])[:16]}")
    for k in ("reason", "note"):
        if d.get(k):
            bits.append(str(d[k]))
    return " · ".join(bits) or None


def admin_audit_entry(row: dict, kind: str, alias_of: "callable",
                      name_of: "callable" = None) -> dict:
    """audit_log_reportable row (+ its LEFT JOINed request columns, prefixed
    `req_`) -> {id, requestId, time, actor, event, target, tier, rows,
    durationMs, query, kind}. Query decisions link a request (which carries the requester, target,
    tier, row count, timing and full SQL — the audit row's own details do not),
    so the table can show who ran what, where, at which tier, plus rows +
    duration + a copyable query. Grants/scopes fall back to details."""
    action = row.get("action") or ""
    event = _EVENT_LABEL.get(action, action.replace("_", " "))
    d = row.get("details") if isinstance(row.get("details"), dict) else {}
    tier = d.get("mode") or d.get("tier") or d.get("max_tier")

    req_tid = row.get("req_target_server_id")
    req_db = row.get("req_database_name")
    req_who = row.get("req_requester_name")
    req_q = row.get("req_query")

    rows = row.get("req_row_count")
    duration_ms = None
    ex, done = row.get("req_executed_at"), row.get("req_completed_at")
    if ex and done:
        try:
            duration_ms = max(0, int((done - ex).total_seconds() * 1000))
        except Exception:
            duration_ms = None
    query = str(req_q) if req_q else None

    if req_tid or req_db or req_who:
        # A query request is linked — "<who> · <alias>/<db>" (+ tier column).
        loc = "/".join(str(b) for b in [alias_of(req_tid) if req_tid else None, req_db] if b)
        target = " · ".join(p for p in [req_who, loc] if p)
    else:
        # No linked request (grants / auto-grants / scopes / kill) — use details.
        tid = d.get("target_id") or d.get("target_server_id")
        alias = d.get("alias") or (alias_of(tid) if tid else None)
        bits = [b for b in [alias, d.get("database") or d.get("database_name")] if b]
        target = "/".join(str(b) for b in bits)
        grantee = d.get("grantee") or d.get("user") or d.get("slack_user_id")
        if grantee and name_of:
            grantee = name_of(grantee)
        if grantee:
            target = f"{grantee}" + (f" → {target}" if target else "")

    actor = row.get("actor_name") or (name_of(row.get("actor_slack_id")) if name_of
                                      else row.get("actor_slack_id")) or "system"
    if actor in ("AUTO", "auto"):
        actor = "auto-approve"
    # Surface which surface (Slack vs web) a query request came from, on the
    # info sub-line — same "via" wording as the admin DM / web queue. Only for
    # query-linked rows (grants/logins have their own info from details).
    info = _audit_info(d)
    origin = row.get("req_origin")
    if (req_tid or req_db or req_who) and origin:
        via = "via web" if str(origin).lower() == "web" else "via Slack"
        info = via if not info else f"{via} · {info}"
    return {"id": str(row["id"]), "time": iso(row.get("created_at")),
            # The REQUEST id, not this audit row's own id. Every query decision
            # belongs to a request, and the number the requester sees in their tab
            # is the one to search the log by; without it the two views of the
            # same event share no visible key. None for entries with no request
            # behind them (grants, scopes, auto-approve windows, kill switch).
            "requestId": (str(row["request_id"])
                          if row.get("request_id") is not None else None),
            "actor": actor, "event": event, "target": target,
            "tier": (str(tier).upper() if tier else None),
            "rows": rows, "durationMs": duration_ms, "query": query,
            "info": info, "kind": kind}


def feedback_entry(row: dict) -> dict:
    """request_ratings_reportable row -> {id, user, score, comment, queryId, when}."""
    return {
        "id": str(row["id"]),
        "user": row.get("name") or row.get("slack_user_id"),
        "score": row.get("rating"),
        "comment": row.get("feedback_text"),
        "queryId": str(row["request_id"]) if row.get("request_id") else None,
        "when": iso(row.get("rated_at")),
    }


def endpoint_request_entry(row: dict, alias_of: "callable") -> dict:
    """One access_requests row → admin endpoint-request shape. `tier` is
    not stored on the row, so it is omitted."""
    return {
        "id": f"er_{row['id']}",
        "requester": row.get("requester_name") or row.get("requester_slack_id"),
        "requesterId": row.get("requester_slack_id"),
        "server": alias_of(row.get("target_server_id"))
        or (str(row["target_server_id"]) if row.get("target_server_id") else None),
        "database": row.get("database_name"),
        "reason": row.get("reason"),
        "status": row.get("status"),
        "requestedAt": iso(row.get("created_at")),
    }


# ---- POST /queries response pieces ------------------------------------------

_PII_LABELS = {
    "email": "Email address", "phone": "Phone number", "tckn": "National ID",
    "vkn": "Tax ID", "iban": "IBAN", "card": "Card number", "name": "Name",
    "address": "Address",
}
_PII_MASK_STYLE = {
    "email": "partial", "phone": "partial", "iban": "partial",
    "card": "partial", "name": "partial",
    "tckn": "full", "vkn": "full", "address": "full",
}


def classification_of(query: str) -> dict:
    """Server-authoritative echo of the frontend's qhClassify shape:
    {tier, statements: [{kw, tier}], multi}."""
    import sqlparse

    from .. import query_safety
    statements = []
    for stmt in sqlparse.split(query or ""):
        s = stmt.strip().rstrip(";").strip()
        if not s:
            continue
        kw = s.split(None, 1)[0].upper()
        statements.append({"kw": kw, "tier": query_safety.required_mode(s).upper()})
    return {
        "tier": query_safety.required_mode(query).upper(),
        "statements": statements,
        "multi": len(statements) > 1,
    }


def pii_preview(query: str) -> dict:
    """Best-effort pre-execution PII hint: which referenced columns match
    the catalog + whether SELECT * appears. The authoritative masking
    happens at delivery; this mirrors it for the submit response."""
    from .. import pii
    cols: list[str] = []
    star = False
    try:
        import sqlglot
        from sqlglot import exp
        for s in sqlglot.parse(query, read="postgres"):
            if s is None:
                continue
            star = star or bool(list(s.find_all(exp.Star)))
            for c in s.find_all(exp.Column):
                if c.name:
                    cols.append(c.name)
    except Exception:
        pass  # unparseable → empty preview; execution-time masking still applies
    seen: dict[str, str] = {}
    pii_map = pii.column_pii_map(cols)
    for idx, ptype in pii_map.items():
        seen.setdefault(cols[idx].lower(), ptype)
    return {
        "columns": [
            {"col": col, "label": _PII_LABELS.get(pt, pt),
             "mask": _PII_MASK_STYLE.get(pt, "full")}
            for col, pt in seen.items()
        ],
        "star": star,
    }


# ---- GET /queries/:id pieces -------------------------------------------------

def run_ms(row: dict) -> int | None:
    a, b = row.get("executed_at"), row.get("completed_at")
    if a and b:
        return int((b - a).total_seconds() * 1000)
    return None


def audit_entries(rows: list[dict], caller_id: str) -> list[dict]:
    """audit_log slice → contract audit[] {time, actor, event}."""
    out = []
    for r in rows:
        actor = r.get("actor_slack_id")
        if actor == caller_id:
            label = "you"
        elif actor == "AUTO":
            label = "auto-approve"
        elif actor is None or actor == "SYSTEM":
            label = "system"
        else:
            label = r.get("actor_name") or actor
        out.append({
            "time": fmt_ts_ms(r.get("created_at")),
            "actor": label,
            "event": (r.get("action") or "").replace("_", " "),
        })
    return out


def status_messages(row: dict) -> list[dict]:
    """Derived, human-readable message feed {time, kind, text} the UI
    shows under the editor. kind ∈ info | ok | err (exact strings)."""
    def t(dt):
        # Full yyyy-MM-dd HH:mm:ss.fff (display tz) — the Messages tab renders
        # this verbatim, matching the audit feed and the design's client stamp.
        return fmt_ts_ms(dt)

    msgs: list[dict] = []
    tier = None
    try:
        from .. import query_safety
        tier = query_safety.required_mode(row["query"]).upper()
    except Exception:
        tier = "?"
    msgs.append({"time": t(row.get("created_at")), "kind": "info",
                 "text": f"Submitted {tier} query for DBA approval."})
    status = row.get("status")
    sf = row.get("scheduled_for")
    if sf and not row.get("executed_at"):
        msgs.append({"time": t(row.get("created_at")), "kind": "info",
                     "text": f"Scheduled for {_to_display(sf):%Y-%m-%d %H:%M} — "
                             "runs automatically; you'll be notified when it's done."})
    if row.get("decided_at"):
        approver = approver_label(row.get("decided_by_slack_id"),
                                  row.get("decided_by_name"))
        if status in ("rejected", "changes_requested"):
            note = row.get("decision_reason") or "No reason given."
            msgs.append({"time": t(row.get("decided_at")), "kind": "err",
                         "text": f"Rejected by {approver}: {note}"})
        elif status == "cancelled":
            msgs.append({"time": t(row.get("decided_at")), "kind": "err",
                         "text": "Cancelled."})
        else:
            msgs.append({"time": t(row.get("decided_at")), "kind": "ok",
                         "text": f"Approved by {approver}."})
    if row.get("executed_at"):
        msgs.append({"time": t(row.get("executed_at")), "kind": "info",
                     "text": "Execution started."})
    if status == "completed":
        n = row.get("row_count")
        msgs.append({"time": t(row.get("completed_at")), "kind": "ok",
                     "text": f"Done — {n if n is not None else '?'} row(s)."})
    elif status == "failed":
        msgs.append({"time": t(row.get("completed_at") or row.get("executed_at")),
                     "kind": "err",
                     "text": row.get("error_message") or "Execution failed."})
    return msgs


# ---- schema tree (SSMS-style) -----------------------------------------------

import re as _re

_IDX_RE = _re.compile(r"CREATE\s+(UNIQUE\s+)?INDEX\s+\S+\s+ON\s+\S+\s+USING\s+\w+\s*\((?P<cols>.+)\)\s*$", _re.I)
_FK_RE = _re.compile(r"FOREIGN KEY\s*\((?P<cols>[^)]+)\)\s*REFERENCES\s+(?P<ref>[^\s(]+)\s*\((?P<refcols>[^)]+)\)", _re.I)


def _split_cols(s: str) -> list[str]:
    return [c.strip().strip('"') for c in s.split(",") if c.strip()]


def parse_index(idx: dict) -> dict:
    """One schema_tables.indexes entry {name, def} → {name, cols, unique, pk}."""
    d = idx.get("def") or ""
    m = _IDX_RE.search(d)
    cols = _split_cols(m.group("cols")) if m else []
    name = idx.get("name") or ""
    return {"name": name, "cols": cols,
            "unique": bool(m and m.group(1)), "pk": name.endswith("_pkey")}


def fk_map(foreign_keys) -> dict[str, str]:
    """table.foreign_keys jsonb → {local_col: 'reftable.refcol'} (schema
    stripped), for the column-level `fk` hint in the schema tree."""
    out: dict[str, str] = {}
    for fk in foreign_keys or []:
        m = _FK_RE.search(fk.get("def") or "")
        if not m:
            continue
        locals_ = _split_cols(m.group("cols"))
        refs = _split_cols(m.group("refcols"))
        ref_tbl = (m.group("ref") or "").split(".")[-1].strip('"')
        for i, lc in enumerate(locals_):
            rc = refs[i] if i < len(refs) else (refs[0] if refs else "")
            out[lc] = f"{ref_tbl}.{rc}"
    return out
