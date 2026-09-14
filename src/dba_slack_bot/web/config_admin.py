"""Fleet-wide configuration surface for the web admin.

The System configuration screen reads and writes the REAL `bot_config` table —
no mock. Every key here is a live setting the bot and/or web app reads at
runtime. Types are explicit: value-based guessing is unreliable (e.g.
`cost_other_monthly_usd = 0` is an integer, not a boolean). Any bot_config key
not listed still surfaces under "Other" as free text, so the screen stays
comprehensive as new keys are added. Writes are type-coerced (preserving each
bool key's existing true/false vocabulary so a direct string compare elsewhere
in the code never breaks) and audited per save. Most keys are runtime-effective;
`log_level` is read once at process start (noted in its row).
"""
from __future__ import annotations

from zoneinfo import available_timezones

from .. import db

# Every valid IANA timezone name, computed once. Used to validate the
# `web_display_timezone` value on save; the UI offers these as a dropdown so
# nobody has to remember/type an exact zone name.
_TZSET = available_timezones()

# key -> control type: "bool" (toggle), "int" (number), "str" (text).
_TYPES = {
    # bool
    "allow_explain_analyze": "bool", "ast_safety_enabled": "bool",
    "batch_enabled": "bool", "csv_import_enabled": "bool",
    "fingerprint_cache_enabled": "bool", "grant_reaper_enabled": "bool",
    "kill_switch": "bool", "mssql_trust_server_cert": "bool",
    "pending_expiry_enabled": "bool", "pii_masking_enabled": "bool",
    "pre_flight_explain": "bool", "query_plan_logging": "bool",
    "rating_enabled": "bool", "require_justification": "bool",
    "security_override_ttl_enabled": "bool", "web_auth_slack_enabled": "bool",
    "web_cookie_secure": "bool",
    # int
    "batch_max_items": "int", "cost_avoided_replicas": "int",
    "cost_dba_hourly_usd": "int", "cost_dba_minutes_per_request": "int",
    "cost_other_monthly_usd": "int", "cost_per_replica_monthly_usd": "int",
    "csv_size_mb": "int", "execution_lease_sec": "int",
    "fingerprint_cache_ttl_days": "int", "grant_idle_revoke_days": "int",
    "import_csv_ttl_hours": "int", "import_max_mb": "int",
    "import_max_rows": "int", "import_timeout_sec": "int",
    "max_open_requests_per_user": "int", "max_rows": "int",
    "max_schedule_days": "int", "min_query_length": "int",
    "pending_expiry_hours": "int", "query_timeout_sec": "int",
    "results_ttl_hours": "int", "risk_high_cost": "int",
    "risk_seq_scan_rows": "int", "ro_burst_threshold": "int",
    "ro_burst_window_min": "int", "ro_window_minutes": "int",
    "security_override_ttl_minutes": "int",
    # str
    "auto_approve_feed_channel": "str", "bot_display_icon": "str",
    "bot_display_name": "str", "kill_switch_message": "str", "log_level": "str",
    "report_start_date": "str", "report_timezone": "str",
    "set_allowed_params": "str", "target_alias_allow_patterns": "str",
    "target_host_allow_patterns": "str", "web_base_url": "str",
    "web_repo_slug": "str",
    # tz — a validated IANA timezone name, rendered as a searchable dropdown.
    "web_display_timezone": "tz",
}

# ordered groups: (id, title, [keys]). Keys not in any group fall to "Other".
_GROUPS = [
    ("web", "Web app", [
        "web_auth_slack_enabled", "web_base_url", "web_cookie_secure",
        "web_display_timezone", "web_repo_slug"]),
    ("approval", "Approval & review", [
        "require_justification", "pending_expiry_enabled", "pending_expiry_hours",
        "max_open_requests_per_user", "min_query_length", "max_schedule_days"]),
    ("grants", "Grants & RO windows", [
        "grant_idle_revoke_days", "grant_reaper_enabled",
        "security_override_ttl_enabled", "security_override_ttl_minutes",
        "ro_burst_threshold", "ro_burst_window_min", "ro_window_minutes",
        "fingerprint_cache_enabled", "fingerprint_cache_ttl_days"]),
    ("execution", "Query execution", [
        "query_timeout_sec", "execution_lease_sec", "max_rows",
        "batch_enabled", "batch_max_items"]),
    ("safety", "Safety & pre-flight", [
        "ast_safety_enabled", "pre_flight_explain", "allow_explain_analyze",
        "query_plan_logging", "risk_high_cost", "risk_seq_scan_rows",
        "set_allowed_params"]),
    ("pii", "Data protection (PII)", ["pii_masking_enabled"]),
    ("targets", "Targets & fleet policy", [
        "target_alias_allow_patterns", "target_host_allow_patterns",
        "mssql_trust_server_cert"]),
    ("csv", "CSV import", [
        "csv_import_enabled", "csv_size_mb", "import_max_mb", "import_max_rows",
        "import_timeout_sec", "import_csv_ttl_hours"]),
    ("slack", "Slack & notifications", [
        "auto_approve_feed_channel", "kill_switch", "kill_switch_message",
        "bot_display_name", "bot_display_icon", "rating_enabled"]),
    ("cost", "Cost model (metrics)", [
        "cost_dba_minutes_per_request", "cost_dba_hourly_usd",
        "cost_avoided_replicas", "cost_per_replica_monthly_usd",
        "cost_other_monthly_usd"]),
    ("reports", "Reports & metrics", ["report_start_date", "report_timezone"]),
    ("retention", "Retention", ["results_ttl_hours"]),
    ("system", "System", ["log_level"]),
]

_TRUTHY = {"1", "true", "yes", "on"}


def _kind(key: str, value) -> str:
    if key in _TYPES:
        return _TYPES[key]
    s = (str(value) if value is not None else "").strip().lower()
    if s in {"on", "off", "true", "false", "yes", "no"}:
        return "bool"
    try:
        int(s)
        return "int"
    except ValueError:
        return "str"


def _label(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def build_config() -> dict:
    """Read bot_config and shape it into typed, grouped items for the screen."""
    rows = db.fetch_all(
        "SELECT key, value, description, updated_at FROM bot_config ORDER BY key")
    by_key = {r["key"]: r for r in rows}
    used: set[str] = set()

    def item(key: str):
        r = by_key.get(key)
        if not r:
            return None
        used.add(key)
        return {
            "key": key, "label": _label(key), "value": r["value"],
            "type": _kind(key, r["value"]), "description": r["description"] or "",
            "updatedAt": r["updated_at"].isoformat() if r["updated_at"] else None,
        }

    groups = []
    for gid, title, keys in _GROUPS:
        items = [it for it in (item(k) for k in keys) if it]
        if items:
            groups.append({"id": gid, "title": title, "items": items})
    other = [it for it in (item(r["key"]) for r in rows if r["key"] not in used) if it]
    if other:
        groups.append({"id": "other", "title": "Other", "items": other})

    values = {r["key"]: r["value"] for r in rows}
    return {"groups": groups, "values": values}


# Int keys where a non-positive value would DISABLE a safety limit (a 0/blank
# timeout means "no timeout", a 0 lease means "reconcile immediately"), so they
# must be >= 1. Other ints are merely required to be non-negative.
_MUST_BE_POSITIVE = frozenset({
    "query_timeout_sec", "execution_lease_sec", "import_timeout_sec",
    "max_rows", "import_max_rows", "min_query_length",
    "max_open_requests_per_user",
})


def _coerce(raw, kind: str, current: str | None, key: str | None = None) -> str | None:
    """Normalize an incoming value to what bot_config should store. Bools keep
    the key's existing true/false vocabulary so a `== 'true'` check elsewhere
    never breaks. Ints must parse and pass a range check (no negatives; the
    safety-limit keys must be positive). Strings pass through (empty allowed)."""
    s = "" if raw is None else str(raw).strip()
    if kind == "bool":
        truthy = s.lower() in _TRUTHY
        cur_l = (current or "").strip().lower()
        if cur_l in {"true", "false"}:
            return "true" if truthy else "false"
        if cur_l in {"1", "0"}:
            return "1" if truthy else "0"
        return "on" if truthy else "off"
    if kind == "int":
        try:
            n = int(float(s))
        except (ValueError, TypeError):
            return None
        if n < 0:
            return None  # config values are never negative
        if n == 0 and key in _MUST_BE_POSITIVE:
            return None  # 0 here would disable a safety limit
        return str(n)
    if kind == "tz":
        # Only a real IANA zone can be saved — a bad value would break the
        # client's Intl.DateTimeFormat. Reject (None = skip) anything else.
        return s if s in _TZSET else None
    return s


def _lease_covers_timeout(pending: dict) -> str | None:
    """Reject a change that would let the orphan reconciler kill live queries.

    `execution_lease_sec` is how long an 'executing' row may sit before it is
    treated as orphaned; `query_timeout_sec` is how long a query may actually
    run. If the lease drops to or below the timeout, a perfectly healthy
    long-running query gets reconciled to 'failed' underneath itself — and the
    two keys are edited independently, so nothing connected them. Requires the
    lease to exceed the timeout with room for result streaming."""
    try:
        lease = int(pending.get("execution_lease_sec"))
        timeout = int(pending.get("query_timeout_sec"))
    except (TypeError, ValueError):
        return None
    if lease <= timeout:
        return (f"execution_lease_sec ({lease}) must be greater than "
                f"query_timeout_sec ({timeout}) — otherwise the orphan "
                f"reconciler fails queries that are still running.")
    if lease < timeout + 30:
        return (f"execution_lease_sec ({lease}) leaves no margin over "
                f"query_timeout_sec ({timeout}); allow at least 30s for "
                f"result streaming and delivery.")
    return None


def apply_config(changes: dict, cur) -> list[dict]:
    """Write the changed keys inside the caller's transaction cursor. Only keys
    that already exist in bot_config are editable (never inject new keys).
    Returns the list of {key, from, to} actually changed, for the audit row.

    Raises ValueError when a change would break a cross-key invariant."""
    cur.execute("SELECT key, value FROM bot_config")
    current = {r["key"]: r["value"] for r in cur.fetchall()}

    # Validate cross-key invariants against the POST-change state, so it holds
    # whether one key or both are being edited in this request.
    post = dict(current)
    for k, raw in (changes or {}).items():
        if k in current:
            v = _coerce(raw, _kind(k, current[k]), current[k], key=k)
            if v is not None:
                post[k] = v
    problem = _lease_covers_timeout(post)
    if problem:
        raise ValueError(problem)

    applied: list[dict] = []
    for key, raw in (changes or {}).items():
        if key not in current:
            continue
        val = _coerce(raw, _kind(key, current[key]), current[key], key=key)
        if val is None or val == current[key]:
            continue
        cur.execute(
            "UPDATE bot_config SET value = %s, updated_at = NOW() WHERE key = %s",
            (val, key))
        applied.append({"key": key, "from": current[key], "to": val})
    return applied
