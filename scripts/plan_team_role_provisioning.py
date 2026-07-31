"""Print a runbook for provisioning per-team Postgres roles on every
target where a team_target_grant exists but target_role is still NULL.

For each (team, target) pair without a role, prints:
  1. The psql command to run on the TARGET cluster (creates the team role,
     grants it to the bot login user, prints follow-up GRANT examples).
  2. Domain-specific GRANTs the DBA must add (placeholder section).
  3. The UPDATE statement for the BOT DB to wire team_target_grants
     .target_role for that pair.

Read-only: this script does not modify either the bot DB or the targets.
It just generates a step-by-step runbook tailored to your current data.

Usage:
    set -a; source /etc/queryhub/env; set +a
    .venv/bin/python scripts/plan_team_role_provisioning.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import db  # noqa: E402


def _role_name(team_name: str) -> str:
    return f"slackbot_team_{team_name.replace('-', '_')}"


def main() -> int:
    db.init_pool()
    rows = db.fetch_all(
        "SELECT t.name AS team, ts.alias AS target, ts.host AS host, "
        "       ts.username AS ro_login, ts.username_rw AS rw_login, "
        "       ts.username_ddl AS ddl_login, "
        "       ts.id AS target_id, t.id AS team_id, g.mode "
        "FROM team_target_grants g "
        "JOIN teams t ON t.id = g.team_id "
        "JOIN target_servers ts ON ts.id = g.target_server_id "
        "WHERE g.target_role IS NULL AND ts.enabled = TRUE "
        "ORDER BY t.name, ts.alias"
    )
    if not rows:
        print("All team_target_grants already have target_role set. "
              "Nothing to provision.")
        return 0

    print(f"# Team-role provisioning runbook — {len(rows)} pair(s) pending")
    print()
    print("For each section: (1) run the psql block on the TARGET cluster "
          "(needs DBA admin password), (2) add the domain-specific GRANTs, "
          "(3) run the UPDATE on the BOT DB.")
    print()

    by_team: dict[str, list[dict]] = {}
    for r in rows:
        by_team.setdefault(r["team"], []).append(r)

    for team, items in by_team.items():
        role = _role_name(team)
        print(f"## team `{team}` (role `{role}`) — {len(items)} target(s)")
        print()
        for r in items:
            print(f"### {r['target']}  (host `{r['host']}`, mode={r['mode']})")
            print()
            print("**Step 1 — provision role on the target cluster:**")
            print()
            print(f"```bash")
            print(f"read -s -p 'admin password: ' PGPASSWORD; echo")
            print(f"export PGPASSWORD")
            # Pick the bot login user matching the grant mode. RO/RW grants
            # both need the role granted to slackbot_ro (and rw if exists).
            bot_logins = [r["ro_login"]]
            if r["rw_login"]:
                bot_logins.append(r["rw_login"])
            if r["ddl_login"] and r["mode"] == "ddl":
                bot_logins.append(r["ddl_login"])
            for bl in bot_logins:
                print(f"psql -h {r['host']} -U <admin> -d postgres \\")
                print(f"     -v role_name={role} \\")
                print(f"     -v bot_login={bl} \\")
                print(f"     -f deploy/grant_team_role.sql")
            print(f"unset PGPASSWORD")
            print(f"```")
            print()
            print("**Step 2 — add team-specific privileges (domain-specific, "
                  "not auto-generated). Example for an RO team:**")
            print()
            print(f"```sql")
            print(f"-- on the target, in each database the team owns:")
            print(f"GRANT CONNECT ON DATABASE <db>     TO {role};")
            print(f"GRANT USAGE   ON SCHEMA <schema>   TO {role};")
            print(f"GRANT SELECT  ON ALL TABLES IN SCHEMA <schema> TO {role};")
            print(f"ALTER DEFAULT PRIVILEGES IN SCHEMA <schema>")
            print(f"    GRANT SELECT ON TABLES TO {role};")
            print(f"```")
            print()
            print("**Step 3 — wire the role on the BOT DB so the executor "
                  "uses SET LOCAL ROLE:**")
            print()
            print(f"```sql")
            print(f"UPDATE team_target_grants SET target_role = '{role}'")
            print(f" WHERE team_id = {r['team_id']}"
                  f" AND target_server_id = {r['target_id']};")
            print(f"```")
            print()
        print("---")
        print()

    print(f"After all steps: re-run this script to confirm zero remaining "
          f"unprovisioned pairs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
