"""Sync target_servers with `inventory.v_all_databases`.

Two-way reconciliation, idempotent, safe to run hourly via cron / job
runner:

  1. ADD — endpoints in inventory but not in target_servers get
     INSERTed (enabled=TRUE, sentinel password) so they appear in the
     /sql admin dropdown immediately. Querying them fails fast with
     an auth error until DBA fills in real credentials.

  2. SWEEP — target_servers rows whose host is NO LONGER in inventory
     get UPDATEd to enabled=FALSE, BUT ONLY for unprovisioned
     placeholders this script created (still carrying the sentinel
     password). Manually-curated targets with real credentials are
     never auto-disabled — they may intentionally live outside
     inventory (e.g. clusters in an AWS account the inventory
     collector can't reach). Never DELETEd (request history + FK
     integrity preserved). Rows already disabled are left alone, so
     manual disables (e.g. the read-replica fence) are respected and
     not re-enabled.

  3. NO RE-ENABLE — when a previously-removed endpoint reappears in
     inventory, the row stays at enabled=FALSE. Bringing a target
     back is a deliberate decision the DBA makes with an explicit
     UPDATE.

Safety: if inventory returns zero endpoints (e.g. transient query
failure), the sweep step is skipped entirely to avoid mass-disabling
everything.

Naming convention:
    alias = first dotted segment of the endpoint
        e.g. acme-prod-orders.<aws-id>.<region>.rds.amazonaws.com
             → acme-prod-orders

For each new row:
    host             = full endpoint
    port             = 5432
    default_database = first non-'postgres' database_name on that endpoint
                       (fallback 'postgres' if only 'postgres' exists)
    username         = 'queryhub_ro'   (matches deploy/grant_readonly.sql)
    password_encrypted = encrypt('PASSWORD_NOT_SET')   sentinel
    enabled          = TRUE
    notes            = 'auto-imported from inventory.v_all_databases — fill creds.'

Usage:
    set -a; source /etc/queryhub/env; set +a
    .venv/bin/python scripts/import_targets_from_inventory.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import psycopg  # noqa: E402

from queryhub import db  # noqa: E402
from queryhub import target_policy  # noqa: E402
from queryhub.config import ENV  # noqa: E402
from queryhub.crypto import decrypt, encrypt  # noqa: E402

log = logging.getLogger("import-targets")

SENTINEL_PASSWORD = "PASSWORD_NOT_SET"
DEFAULT_USERNAME = "queryhub_ro"


def _existing_hosts() -> set[str]:
    rows = db.fetch_all("SELECT host FROM target_servers")
    return {r["host"] for r in rows}


def _inventory_endpoints() -> list[tuple[str, list[str]]]:
    """Return [(endpoint, [database_name, ...sorted, postgres last]), ...]."""
    with psycopg.connect(
        host=ENV.bot_db_host,
        port=ENV.bot_db_port,
        dbname="inventory",
        user=ENV.bot_db_user,
        password=ENV.bot_db_password,
        connect_timeout=10,
        application_name="dba-slack-bot:bulk-import",
    ) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT endpoint, database_name FROM v_all_databases "
            "ORDER BY endpoint, "
            "         (database_name = 'postgres') ASC, "  # non-postgres first
            "         database_name"
        )
        from collections import defaultdict
        grouped: dict[str, list[str]] = defaultdict(list)
        for endpoint, db_name in cur.fetchall():
            grouped[endpoint].append(db_name)
        return list(grouped.items())


def _inventory_servers() -> list[dict]:
    """Instance-level rows from inventory.v_server, INCLUDING soft-deleted
    ones. v_all_databases filters `is_deleted = false`, so a deleted
    instance simply vanishes from it — indistinguishable from an instance
    the collector can't reach. v_server keeps the explicit deletion
    signal (is_deleted / deleted_at), which is what the authoritative
    sweep below consumes."""
    with psycopg.connect(
        host=ENV.bot_db_host,
        port=ENV.bot_db_port,
        dbname="inventory",
        user=ENV.bot_db_user,
        password=ENV.bot_db_password,
        connect_timeout=10,
        application_name="dba-slack-bot:bulk-import",
    ) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT db_instance_identifier, endpoint, is_deleted, deleted_at "
            "FROM v_server"
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def plan_authoritative_disables(servers: list[dict],
                                targets: list[dict]) -> list[dict]:
    """Enabled targets that inventory says are GONE — not merely absent.

    Two authoritative signals (pure function; unit-tested):

      A. deleted: the target's host matches a v_server row with
         is_deleted = true. The collector saw AWS delete the instance.
      B. replaced: the target's host is unknown to v_server entirely,
         but the instance identifier derived from the host (first dotted
         segment) is alive in v_server at a DIFFERENT endpoint — the
         identifier was re-used (e.g. a service migrated engines), so
         the old endpoint is dead for sure.

    Plain absence from inventory stays untouched — the collector may
    simply not reach that account. Returns [{id, alias, host, reason,
    detail}, ...]; the caller applies + audits + notifies.
    """
    by_endpoint = {s["endpoint"]: s for s in servers if s.get("endpoint")}
    live_by_identifier: dict[str, dict] = {}
    for s in servers:
        if not s.get("is_deleted") and s.get("endpoint"):
            live_by_identifier.setdefault(s["db_instance_identifier"], s)

    plans: list[dict] = []
    for t in targets:
        row = by_endpoint.get(t["host"])
        if row is not None:
            if row.get("is_deleted"):
                plans.append({
                    "id": t["id"], "alias": t["alias"], "host": t["host"],
                    "reason": "inventory reports the instance deleted",
                    "detail": f"deleted_at={row.get('deleted_at')}",
                })
            continue  # endpoint known and alive → nothing to do
        identifier = t["host"].split(".", 1)[0]
        live = live_by_identifier.get(identifier)
        if live is not None and live["endpoint"] != t["host"]:
            plans.append({
                "id": t["id"], "alias": t["alias"], "host": t["host"],
                "reason": "instance identifier now lives at a different endpoint",
                "detail": f"now at {live['endpoint']}",
            })
    return plans


def _notify_admins_disabled(plans: list[dict]) -> None:
    """DM every active admin about auto-disabled targets. Best-effort:
    a Slack hiccup must not fail the sync run (the disable itself is
    already committed + audited)."""
    try:
        from slack_sdk.web import WebClient

        from queryhub import admins

        client = WebClient(token=ENV.slack_bot_token)
        lines = "\n".join(
            f"• `{p['alias']}` — {p['reason']} ({p['detail']})"
            for p in plans
        )
        text = (
            f":broom: *Inventory sweep disabled {len(plans)} target(s)* "
            f"whose instance inventory explicitly reports gone:\n{lines}\n"
            f"_Grants and request history are preserved; re-enable with an "
            f"explicit UPDATE if this is wrong._"
        )
        for admin in admins.list_active():
            opened = client.conversations_open(users=admin["slack_user_id"])
            client.chat_postMessage(channel=opened["channel"]["id"], text=text)
    except Exception:
        log.exception("admin DM for auto-disabled targets failed (non-fatal)")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db.init_pool()

    existing = _existing_hosts()
    log.info("target_servers already has %d hosts", len(existing))

    endpoints = _inventory_endpoints()
    log.info("inventory has %d distinct endpoints", len(endpoints))

    # Hard safety: zero endpoints almost certainly means the inventory
    # query failed (or the view is empty for an unrelated reason).
    # Refuse to sweep — a mass-disable would be very hard to undo.
    if not endpoints:
        log.error("inventory returned zero endpoints — aborting (refusing to "
                  "sweep and risk mass-disabling target_servers)")
        return 1

    sentinel_ct = encrypt(SENTINEL_PASSWORD)
    inserted = skipped = 0

    # ---- 1. ADD: insert new endpoints ----
    inventory_hosts: set[str] = set()
    for endpoint, db_names in endpoints:
        inventory_hosts.add(endpoint)
        if endpoint in existing:
            skipped += 1
            continue
        alias = endpoint.split(".", 1)[0]
        non_pg = [d for d in db_names if d != "postgres"]
        default_db = non_pg[0] if non_pg else "postgres"
        # New endpoints are always imported DISABLED (with a sentinel
        # password). Enabling one is a deliberate act — fill real creds,
        # then enable (a provisioning step or a cloud migration). This keeps
        # unprovisioned sentinels out of the picker even when a broad
        # host-allow policy (e.g. *.myhuaweicloud.com) would "want" them.
        db.execute(
            "INSERT INTO target_servers "
            "(alias, host, port, default_database, username, "
            " password_encrypted, enabled, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s, FALSE, %s)",
            (
                alias,
                endpoint,
                5432,
                default_db,
                DEFAULT_USERNAME,
                sentinel_ct,
                "auto-imported from inventory.v_all_databases — fill creds.",
            ),
        )
        log.info("added %s -> %s (default db=%s)", alias, endpoint, default_db)
        inserted += 1

    # ---- 2. SWEEP: disable stale auto-imported placeholders ----
    # Only touches enabled=TRUE rows so manual disables (e.g. the
    # read-replica fence) are preserved. No DELETE — request history
    # and FK references must stay intact.
    #
    # A row is swept only when BOTH hold:
    #   (a) its host is no longer in inventory, AND
    #   (b) it still carries the sentinel password — i.e. it is an
    #       unprovisioned placeholder THIS script created and nobody
    #       has filled real credentials for.
    # Targets with real credentials are deliberately set up by a human
    # and may live outside inventory (e.g. an AWS account the inventory
    # collector cannot reach), so they are never auto-disabled.
    stale = db.fetch_all(
        "SELECT id, alias, host, password_encrypted "
        "FROM target_servers WHERE enabled = TRUE"
    )
    disabled = 0
    for r in stale:
        if r["host"] in inventory_hosts:
            continue
        try:
            is_placeholder = decrypt(r["password_encrypted"]) == SENTINEL_PASSWORD
        except Exception:
            # Undecryptable password -> not a sentinel we created; leave it.
            is_placeholder = False
        if not is_placeholder:
            log.info("keeping %s (host=%s) — not in inventory but has real "
                     "credentials (manually curated, not a placeholder)",
                     r["alias"], r["host"])
            continue
        db.execute(
            "UPDATE target_servers SET enabled = FALSE WHERE id = %s",
            (r["id"],),
        )
        log.info("disabled %s (host=%s) — unprovisioned placeholder no "
                 "longer in inventory", r["alias"], r["host"])
        disabled += 1

    # ---- 3. AUTHORITATIVE DISABLE: inventory explicitly says gone ----
    # Unlike step 2 this also covers targets with REAL credentials,
    # because the signal is not "absent from inventory" (collector blind
    # spot, hands off) but v_server explicitly reporting the instance
    # deleted / its identifier re-used at another endpoint. Same safety
    # rails: enabled-only, no DELETE, no re-enable, audited, admins DMed.
    servers = _inventory_servers()
    auto_disabled = 0
    if not servers:
        log.warning("v_server returned zero rows — skipping the "
                    "authoritative sweep this run")
    else:
        enabled_targets = db.fetch_all(
            "SELECT id, alias, host FROM target_servers WHERE enabled = TRUE"
        )
        plans = plan_authoritative_disables(servers, enabled_targets)
        for p in plans:
            db.execute(
                "UPDATE target_servers "
                "SET enabled = FALSE, updated_at = NOW(), "
                "    notes = COALESCE(notes, '') || %s "
                "WHERE id = %s AND enabled = TRUE",
                (f" | auto-disabled by inventory sweep: {p['reason']}.",
                 p["id"]),
            )
            db.execute(
                "INSERT INTO audit_log (actor_slack_id, actor_name, action, "
                " details) "
                "VALUES ('inventory-sync', 'inventory sweep', "
                "        'target_disabled', %s::jsonb)",
                (json.dumps(p),),
            )
            log.info("auto-disabled %s (host=%s) — %s (%s)",
                     p["alias"], p["host"], p["reason"], p["detail"])
            auto_disabled += 1
        if plans:
            _notify_admins_disabled(plans)

    # ---- 4. ALIAS POLICY: disable enabled targets the operator's glob
    # policy (bot_config.target_alias_allow/deny_patterns) doesn't want.
    # Opt-in: no-op unless a pattern is configured. Never enables.
    policy_disabled = target_policy.enforce(commit=True)
    for v in policy_disabled:
        log.info("policy-disabled %s (alias not wanted)", v["alias"])

    log.info("done: inserted=%d disabled=%d auto_disabled=%d "
             "policy_disabled=%d skipped=%d",
             inserted, disabled, auto_disabled, len(policy_disabled), skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
