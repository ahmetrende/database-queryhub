#!/usr/bin/env python3
"""Record which team owns which target, from a CSV.

    team,target
    payments-be,orders-db
    payments-be,ledger-db
    identity-be,orders-db      <- shared, and that is allowed

`team` is `team.name`; `target` is a `target_servers.alias` or its host. The
CSV is the whole interface, for the same reason `import_teams.py` uses one:
whatever knows the ownership is company-specific and stays outside this repo.

WHY THIS EXISTS. A target used to be an independent object — the link to a
team was a hostname string joined at query time, which no screen could show
and nobody could correct. `target_team` makes it a relation; this fills it.

MANY-TO-MANY ON PURPOSE. A database shared by two teams gets a row for each.
Both leads can then approve their OWN team's requests to it, and neither can
approve the other's, which is the right answer and the one a single
`owner_team_id` could not have expressed.

IT GRANTS NOTHING. A row here says who is responsible for a database. What
anyone may read stays in `access_grant`, decided by a person.

IT ONLY OWNS ITS OWN ROWS. `--source` marks them; a link somebody made by
hand (`source IS NULL`) is never touched, which is what makes hand-linking a
target the portal has never heard of a durable thing to do.

    python3 scripts/sync_target_owners.py --csv owns.csv --source pod-sync
    python3 scripts/sync_target_owners.py --csv owns.csv --source pod-sync --apply
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import audit, db  # noqa: E402


def read_csv(path: Path) -> tuple[list[tuple[str, str]], list[str]]:
    pairs: list[tuple[str, str]] = []
    bad: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = {(c or "").strip().lower() for c in (reader.fieldnames or [])}
        for need in ("team", "target"):
            if need not in cols:
                raise SystemExit(f"CSV needs a '{need}' column; found: "
                                 f"{sorted(cols) or 'nothing'}")
        for n, row in enumerate(reader, start=2):
            t, g = (row.get("team") or "").strip(), (row.get("target") or "").strip()
            if not t or not g:
                bad.append(f"line {n}: team={t!r} target={g!r}")
                continue
            pairs.append((t, g))
    return pairs, bad


def plan(cur, source: str, pairs):
    notes: list[str] = []
    wanted: set[tuple[int, int]] = set()
    label: dict[tuple[int, int], str] = {}
    for team_name, target in sorted(set(pairs)):
        cur.execute("SELECT id, display_name FROM team "
                    " WHERE name = %s AND NOT is_deleted", (team_name,))
        team = cur.fetchone()
        if team is None:
            notes.append(f"no team named '{team_name}' — run import_teams first")
            continue
        cur.execute("SELECT id, alias FROM target_servers "
                    " WHERE alias = %s OR lower(host) = lower(%s)", (target, target))
        tgt = cur.fetchone()
        if tgt is None:
            notes.append(f"no target '{target}' (claimed by {team_name})")
            continue
        key = (tgt["id"], team["id"])
        wanted.add(key)
        label[key] = f"{tgt['alias']} → {team['display_name']}"

    cur.execute("SELECT target_id, team_id FROM target_team WHERE source = %s",
                (source,))
    live = {(r["target_id"], r["team_id"]) for r in cur.fetchall()}
    return {"add": sorted(wanted - live, key=lambda k: label.get(k, "")),
            "drop": sorted(live - wanted), "label": label, "notes": notes}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--source", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--actor", default="")
    a = ap.parse_args()
    if not a.source.strip():
        raise SystemExit("--source names the rows this owns; it cannot be empty.")

    pairs, bad = read_csv(a.csv)
    for line in bad:
        print(f"  skipped {line}")

    with db.transaction() as cur:
        p = plan(cur, a.source, pairs)
        print(f"\nsource '{a.source}' — {len(set(pairs))} pair(s) in the CSV")
        for k in p["add"]:
            print(f"  +  {p['label'].get(k, k)}")
        for k in p["drop"]:
            print(f"  -  target {k[0]} no longer claimed by team {k[1]}")
        for n in p["notes"]:
            print(f"  !  {n}")
        if not (p["add"] or p["drop"]):
            print("  nothing to change")

        if not a.apply:
            print("\ndry run — nothing written. Re-run with --apply.")
            cur.connection.rollback()
            return 0

        for tid, team_id in p["drop"]:
            cur.execute("DELETE FROM target_team WHERE target_id = %s "
                        "  AND team_id = %s AND source = %s",
                        (tid, team_id, a.source))
        for tid, team_id in p["add"]:
            cur.execute(
                "INSERT INTO target_team (target_id, team_id, source, created_by) "
                "VALUES (%s,%s,%s,"
                "        (SELECT p.id FROM principal p "
                "           JOIN principal_identity i ON i.principal_id = p.id "
                "          WHERE i.provider='slack' AND i.external_id = %s "
                "            AND NOT i.is_deleted LIMIT 1)) "
                "ON CONFLICT DO NOTHING", (tid, team_id, a.source, a.actor))
        audit.log_in(cur, None, a.actor or a.source, a.actor or a.source,
                     "target_owners_synced",
                     {"source": a.source, "csv": str(a.csv),
                      "added": len(p["add"]), "removed": len(p["drop"]),
                      "notes": p["notes"]})
        print("\napplied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
