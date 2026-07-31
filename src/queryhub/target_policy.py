"""Config-driven target enablement policy — modular allow/deny globs over
BOTH the target alias and its host, so the fleet can be curated by naming
(``exc-*``, ``pass-*``), by cloud / host (``*.myhuaweicloud.com``), or any
mix. Adding a whole cloud is one host pattern, not one alias per service.

bot_config glob keys (comma / whitespace / newline separated; empty = unset):

  target_alias_allow_patterns    target_host_allow_patterns
  target_alias_deny_patterns     target_host_deny_patterns

A target is *wanted* when it passes deny (its alias matches NO alias-deny
AND its host matches NO host-deny) AND, if any allow pattern is set at all
(alias or host), its alias OR host matches one. With no allow patterns set,
everything not denied is wanted. **Deny always beats allow.**

Enforcement (the hourly inventory sync): `enforce()` DISABLES enabled
targets that aren't wanted. It never enables — new endpoints are imported
disabled, and enabling one is always a deliberate act (provisioning or a
cloud migration). So a host-allow can't accidentally light up an
unprovisioned sentinel target; it only keeps deliberately-enabled ones from
being swept.

Adding a new match dimension later (e.g. by tag) is a new key + one line in
`is_wanted` — the shape is intentionally uniform.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re

from . import config as cfg
from . import db

log = logging.getLogger(__name__)

# field -> bot_config key. `field` is "<alias|host>_<allow|deny>".
_KEYS = {
    "alias_allow": "target_alias_allow_patterns",
    "alias_deny": "target_alias_deny_patterns",
    "host_allow": "target_host_allow_patterns",
    "host_deny": "target_host_deny_patterns",
}


def patterns(field: str) -> list[str]:
    """Glob patterns configured for one field (e.g. 'host_allow')."""
    raw = cfg.get_setting(_KEYS[field], "") or ""
    return [p for p in re.split(r"[,\s]+", raw.strip()) if p]


def _match(value: str | None, pats: list[str]) -> bool:
    v = (value or "").strip()
    return any(fnmatch.fnmatch(v, p) for p in pats)


def is_enforced() -> bool:
    """True when any allow/deny pattern (alias or host) is configured."""
    return any(patterns(f) for f in _KEYS)


def is_wanted(alias: str, host: str | None = None) -> bool:
    """Should this target be enabled under the current policy? Deny beats
    allow; an empty allowlist (both alias and host) allows everything not
    denied."""
    if _match(alias, patterns("alias_deny")) or _match(host, patterns("host_deny")):
        return False
    allow_alias = patterns("alias_allow")
    allow_host = patterns("host_allow")
    if not allow_alias and not allow_host:
        return True
    return _match(alias, allow_alias) or _match(host, allow_host)


def unwanted_enabled() -> list[dict]:
    """Enabled targets the policy does not want (empty when not enforced)."""
    if not is_enforced():
        return []
    rows = db.fetch_all(
        "SELECT id, alias, host FROM target_servers WHERE enabled ORDER BY alias")
    return [r for r in rows if not is_wanted(r["alias"], r["host"])]


def enforce(*, commit: bool,
            actor: str = "inventory-sync",
            actor_name: str = "target alias policy") -> list[dict]:
    """Disable every enabled target the policy doesn't want. Returns the
    affected [{id, alias, host}]. Dry-run (commit=False) just returns the
    list. Never enables anything. Each disable appends to notes + writes an
    audit_log row."""
    victims = unwanted_enabled()
    if not (commit and victims):
        return victims
    with db.transaction() as cur:
        for v in victims:
            cur.execute(
                "UPDATE target_servers SET enabled = FALSE, updated_at = NOW(), "
                "  notes = COALESCE(notes, '') || %s "
                "WHERE id = %s AND enabled = TRUE",
                (" | disabled by target policy.", v["id"]),
            )
            cur.execute(
                "INSERT INTO audit_log (actor_slack_id, actor_name, action, "
                " details) VALUES (%s, %s, 'target_disabled', %s::jsonb)",
                (actor, actor_name,
                 json.dumps({"target_id": v["id"], "alias": v["alias"],
                             "reason": "target policy"})),
            )
    log.info("target policy disabled %d target(s): %s",
             len(victims), ", ".join(v["alias"] for v in victims))
    return victims
