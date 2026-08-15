"""Seed an initial team rollout (read-only, team-leads only).

TEMPLATE: the data tables below contain placeholder examples only. Edit
TEAMS / TEAM_LEADS / GRANTS to match your environment before running, or
copy this file out of the repo and customize it locally — the real seed
data should not be committed.

Idempotent: every INSERT uses ON CONFLICT DO NOTHING, so re-running picks up
new rows without disturbing existing ones. Safe to extend the data tables
below and re-run.

Targets get:
  - Added to `requesters` (allowlist gate); admins still bypass
  - Added to `team_members` for their team
  - Their team gets `team_target_grants` for each mapped RDS

If `target_servers.password_encrypted` still holds the sentinel from the
inventory bulk-import, leads see the targets in the modal but get a
"Target X is not ready yet" error on submit until real creds are filled.

Usage:
    set -a; source /etc/queryhub/env; set +a
    .venv/bin/python scripts/seed_initial_teams.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import db  # noqa: E402

log = logging.getLogger("seed-teams")

# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------

# PLACEHOLDER DATA — replace with your real teams / leads / target aliases
# before running. Target aliases must already exist in `target_servers`
# (see scripts/import_targets_from_inventory.py).

TEAMS: list[tuple[str, str]] = [
    # (name, description)
    ("team-a",  "Team A squad"),
    ("team-b",  "Team B squad"),
]

# (team_name, slack_user_id, display_name)
TEAM_LEADS: list[tuple[str, str, str]] = [
    ("team-a",  "U00EXAMPLE01", "Person One"),
    ("team-a",  "U00EXAMPLE02", "Person Two"),
    ("team-b",  "U00EXAMPLE03", "Person Three"),
]

# (team_name, target_alias). NULL allowed_databases — entire RDS.
GRANTS: list[tuple[str, str]] = [
    ("team-a",  "acme-prod-service-1"),
    ("team-a",  "acme-prod-service-2"),
    ("team-b",  "acme-prod-service-3"),
]


# ----------------------------------------------------------------------------
# Operations
# ----------------------------------------------------------------------------

def _seed_teams() -> dict[str, int]:
    """INSERT or fetch each team; return {name: id}."""
    out = {}
    for name, description in TEAMS:
        db.execute(
            "INSERT INTO teams (name, description) VALUES (%s, %s) "
            "ON CONFLICT (name) DO NOTHING",
            (name, description),
        )
        row = db.fetch_one("SELECT id FROM teams WHERE name = %s", (name,))
        if row is None:
            raise RuntimeError(f"failed to find team {name!r} after insert")
        out[name] = row["id"]
        log.info("team %s -> id=%d", name, row["id"])
    return out


def _seed_requesters() -> int:
    """Add each lead to the requesters allowlist. Idempotent + re-enables."""
    inserted = 0
    for _team, slack_id, display_name in TEAM_LEADS:
        before = db.fetch_one(
            "SELECT 1 FROM requesters WHERE slack_user_id = %s", (slack_id,),
        )
        db.execute(
            "INSERT INTO requesters (slack_user_id, name, added_by) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (slack_user_id) DO UPDATE "
            "SET enabled = TRUE, name = COALESCE(requesters.name, EXCLUDED.name)",
            (slack_id, display_name, "seed-initial-teams"),
        )
        if before is None:
            inserted += 1
            log.info("requester added: %s (%s)", slack_id, display_name)
        else:
            log.info("requester already present: %s (re-enabled)", slack_id)
    return inserted


def _seed_team_members(team_ids: dict[str, int]) -> int:
    inserted = 0
    for team_name, slack_id, _name in TEAM_LEADS:
        team_id = team_ids[team_name]
        before = db.fetch_one(
            "SELECT 1 FROM team_members WHERE team_id = %s AND slack_user_id = %s",
            (team_id, slack_id),
        )
        db.execute(
            "INSERT INTO team_members (team_id, slack_user_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (team_id, slack_id),
        )
        if before is None:
            inserted += 1
            log.info("member added: team=%s user=%s", team_name, slack_id)
    return inserted


def _seed_grants(team_ids: dict[str, int]) -> tuple[int, list[str]]:
    inserted = 0
    missing_targets: list[str] = []
    for team_name, alias in GRANTS:
        target = db.fetch_one(
            "SELECT id FROM target_servers WHERE alias = %s", (alias,),
        )
        if target is None:
            log.warning("grant skipped — target alias %r not in target_servers", alias)
            missing_targets.append(alias)
            continue
        team_id = team_ids[team_name]
        before = db.fetch_one(
            "SELECT 1 FROM team_target_grants "
            "WHERE team_id = %s AND target_server_id = %s",
            (team_id, target["id"]),
        )
        db.execute(
            "INSERT INTO team_target_grants "
            "(team_id, target_server_id, allowed_databases, target_role) "
            "VALUES (%s, %s, NULL, NULL) "
            "ON CONFLICT DO NOTHING",
            (team_id, target["id"]),
        )
        if before is None:
            inserted += 1
            log.info("grant added: team=%s target=%s", team_name, alias)
    return inserted, missing_targets


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db.init_pool()
    team_ids = _seed_teams()
    requesters_added = _seed_requesters()
    members_added = _seed_team_members(team_ids)
    grants_added, missing = _seed_grants(team_ids)
    print()
    print("=== summary ===")
    print(f"  teams:      {len(team_ids)} ({list(team_ids.keys())})")
    print(f"  requesters: +{requesters_added} new "
          f"(total leads in this seed: {len(TEAM_LEADS)})")
    print(f"  members:    +{members_added} new")
    print(f"  grants:     +{grants_added} new "
          f"(total mapped: {len(GRANTS) - len(missing)})")
    if missing:
        print(f"  WARNING:    {len(missing)} target alias(es) not found in "
              f"target_servers — grants skipped:")
        for a in missing:
            print(f"              - {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
