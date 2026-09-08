#!/usr/bin/env python3
"""Import an org structure into the access model's `team` / `team_member`.

Takes a CSV of "who is in which team" and reconciles it into the nine-table
model. Deliberately generic: the CSV is the whole interface, so whatever
produces it — an HR export, a directory dump, a company-specific collector —
stays outside this repo and outside this script.

    team,display_name,email,is_lead
    cus-1-be,CUS 1 - BE,berk.tas@example.com,no
    cus-1-be,CUS 1 - BE,emre.tepe@example.com,yes

`team` is the stable code and lands in `team.name`; `display_name` is what
people read. Both `display_name` and `is_lead` are optional columns.

WHAT IT DOES NOT DO, and why each one is deliberate:

* **It never creates a principal.** Somebody in the org chart who has no
  QueryHub account is reported as unresolved, not invented. Being named in a
  directory is not a request for access, and a sync that can add people to the
  access model is a sync that can be used to add people to the access model.
* **It never writes a grant or a role.** These teams describe who works
  together. What they may reach is a separate decision, made in QueryHub by a
  person. An org sync that could widen access would make the org chart a
  permission surface.
* **It only touches teams of its own `--source`.** Teams created by hand
  (`source = 'manual'`, which is also what the migration-109 mirror maintains
  from the legacy tables) are never read, edited or removed. Run it twice with
  two sources and the two structures sit side by side.

Membership changes normally DM the person (migration 060). This suppresses
that by default: a team from here carries no grants, so "you were added to
team X" would announce a change to somebody's access that did not happen. The
day such a team is granted something, that grant's own message is the honest
one. `--notify` opts back in.

    python3 scripts/import_teams.py --csv teams.csv --source pod-sync
    python3 scripts/import_teams.py --csv teams.csv --source pod-sync --apply
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from queryhub import audit, db  # noqa: E402

_TRUE = {"yes", "y", "true", "1", "lead"}


def read_csv(path: Path) -> tuple[dict[str, str], dict[str, list[tuple[str, bool]]], list[str]]:
    """(display name per team, members per team, malformed row descriptions)."""
    names: dict[str, str] = {}
    members: dict[str, list[tuple[str, bool]]] = {}
    bad: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = {(c or "").strip().lower() for c in (reader.fieldnames or [])}
        for need in ("team", "email"):
            if need not in cols:
                raise SystemExit(f"CSV needs a '{need}' column; found: "
                                 f"{sorted(cols) or 'nothing'}")
        for n, row in enumerate(reader, start=2):
            get = lambda k: (row.get(k) or "").strip()  # noqa: E731
            team, email = get("team"), get("email").lower()
            if not team or not email:
                bad.append(f"line {n}: team={team!r} email={email!r}")
                continue
            names.setdefault(team, get("display_name") or team)
            if get("display_name"):
                names[team] = get("display_name")
            members.setdefault(team, [])
            members[team].append((email, get("is_lead").lower() in _TRUE))
    return names, members, bad


def resolve(cur, emails: set[str]) -> dict[str, int]:
    """email -> principal id, for the people QueryHub already knows."""
    if not emails:
        return {}
    cur.execute(
        "SELECT id, lower(email) AS em FROM principal "
        " WHERE NOT is_deleted AND email IS NOT NULL "
        "   AND lower(email) = ANY(%s)", (sorted(emails),))
    return {r["em"]: r["id"] for r in cur.fetchall()}


def plan(cur, source: str, names, members, by_email, keep_empty=False):
    """What would change, without changing it."""
    cur.execute(
        "SELECT id, name, display_name FROM team "
        " WHERE source = %s AND NOT is_deleted", (source,))
    live = {r["name"]: r for r in cur.fetchall()}

    rename = [t for t in names
              if t in live and live[t]["display_name"] != names[t]]
    # Absent from the CSV entirely — not merely empty. A team whose last
    # QueryHub member left is still a team; deleting it would silently
    # un-scope any approver pointed at it.
    drop_teams = [t for t in live if t not in names]

    want: dict[str, set[int]] = {}
    leads: dict[str, set[int]] = {}
    unresolved: set[str] = set()
    # (filled below, then read by add_teams)
    for team, rows in members.items():
        want[team], leads[team] = set(), set()
        for email, is_lead in rows:
            pid = by_email.get(email)
            if pid is None:
                unresolved.add(email)
                continue
            want[team].add(pid)
            if is_lead:
                leads[team].add(pid)

    # A team nobody here belongs to is not created, unless asked for. It
    # grants nothing and decides nothing, and every one of them is another
    # row in the Teams list and another option in the picker that scopes an
    # approver — 15 of 28, on the fleet this was written for. The next run
    # creates it the moment somebody in it has an account, so nothing is lost
    # by waiting. An EXISTING team that empties out is left alone above.
    add_teams = [t for t in names
                 if t not in live and (keep_empty or want.get(t))]

    add_m: list[tuple[str, int]] = []
    del_m: list[tuple[str, int]] = []
    lead_m: list[tuple[str, int, bool]] = []
    for team, wanted in want.items():
        row = live.get(team)
        if row is None and team not in add_teams:
            continue                      # empty and not being created
        have: dict[int, bool] = {}
        if row:
            cur.execute("SELECT principal_id, is_lead FROM team_member "
                        " WHERE team_id = %s AND NOT is_deleted", (row["id"],))
            have = {r["principal_id"]: r["is_lead"] for r in cur.fetchall()}
        for pid in sorted(wanted - set(have)):
            add_m.append((team, pid))
        for pid in sorted(set(have) - wanted):
            del_m.append((team, pid))
        for pid in sorted(wanted & set(have)):
            if have[pid] != (pid in leads[team]):
                lead_m.append((team, pid, pid in leads[team]))
    # `team_name_uq` is unique across every source, so a code that collides
    # with somebody else's team is a unique violation mid-transaction rather
    # than a sentence. Name it here instead. (Zero collisions on the fleet
    # this was written for — the guard is for the next source.)
    if add_teams:
        cur.execute(
            "SELECT name, source FROM team "
            " WHERE name = ANY(%s) AND source <> %s AND NOT is_deleted",
            (add_teams, source))
        clash = cur.fetchall()
        if clash:
            raise SystemExit(
                "these team codes already belong to another source:\n  "
                + "\n  ".join(f"{r['name']} (source={r['source']})"
                               for r in clash))

    # A team named by a live role is not this script's to remove. Somebody
    # decided "approves for this team"; dropping it out from under them turns
    # that row into a scope matching nobody, silently. Reported and skipped.
    pinned = []
    if drop_teams:
        cur.execute(
            "SELECT t.name, COUNT(*) n FROM role_assignment ra "
            "  JOIN team t ON t.id = ra.scope_team_id "
            " WHERE t.name = ANY(%s) AND t.source = %s "
            "   AND ra.revoked_at IS NULL AND NOT ra.is_deleted "
            "   AND (ra.valid_until IS NULL OR ra.valid_until > NOW()) "
            " GROUP BY 1", (drop_teams, source))
        pinned = [(r["name"], r["n"]) for r in cur.fetchall()]
        keep = {n for n, _ in pinned}
        drop_teams = [t for t in drop_teams if t not in keep]

    return {"live": live, "add_teams": add_teams, "rename": rename,
            "drop_teams": drop_teams, "pinned": pinned, "want": want,
            "leads": leads, "add_members": add_m, "del_members": del_m,
            "lead_changes": lead_m, "unresolved": sorted(unresolved)}


def apply(cur, source: str, names, p) -> None:
    for t in p["add_teams"]:
        cur.execute(
            "INSERT INTO team (name, display_name, source, external_id) "
            "VALUES (%s,%s,%s,%s) RETURNING id", (t, names[t], source, t))
        p["live"][t] = {"id": cur.fetchone()["id"], "name": t,
                        "display_name": names[t]}
    for t in p["rename"]:
        cur.execute("UPDATE team SET display_name = %s, updated_at = now() "
                    " WHERE id = %s", (names[t], p["live"][t]["id"]))
    for t, pid in p["add_members"]:
        cur.execute(
            "INSERT INTO team_member (team_id, principal_id, is_lead) "
            "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (p["live"][t]["id"], pid, pid in p["leads"].get(t, set())))
    for t, pid in p["del_members"]:
        cur.execute("DELETE FROM team_member WHERE team_id = %s "
                    "  AND principal_id = %s", (p["live"][t]["id"], pid))
    for t, pid, is_lead in p["lead_changes"]:
        cur.execute("UPDATE team_member SET is_lead = %s, updated_at = now() "
                    " WHERE team_id = %s AND principal_id = %s",
                    (is_lead, p["live"][t]["id"], pid))
    # A team this source no longer lists is soft-deleted, not dropped: its id
    # may be named by a role_assignment, and ON DELETE RESTRICT would refuse
    # anyway. Members go first so the team does not read as populated.
    for t in p["drop_teams"]:
        tid = p["live"][t]["id"]
        cur.execute("DELETE FROM team_member WHERE team_id = %s", (tid,))
        cur.execute("UPDATE team SET is_deleted = TRUE, deleted_at = now(), "
                    "       enabled = FALSE WHERE id = %s", (tid,))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--source", required=True,
                    help="owns the teams it writes; 'manual' is refused")
    ap.add_argument("--apply", action="store_true",
                    help="without it, nothing is written")
    ap.add_argument("--keep-empty", action="store_true",
                    help="create teams nobody here belongs to as well")
    ap.add_argument("--notify", action="store_true",
                    help="let the membership DMs go out (off by default)")
    ap.add_argument("--actor", default="import_teams",
                    help="who the audit row names")
    a = ap.parse_args()

    if a.source == "manual":
        raise SystemExit("'manual' is the hand-made and mirrored structure. "
                         "Pick a source of your own so this cannot touch it.")

    names, members, bad = read_csv(a.csv)
    for line in bad:
        print(f"  skipped {line}")

    with db.transaction() as cur:
        if not a.notify:
            cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        emails = {e for rows in members.values() for e, _ in rows}
        by_email = resolve(cur, emails)
        p = plan(cur, a.source, names, members, by_email, a.keep_empty)

        print(f"\nsource '{a.source}' — {len(names)} teams in the CSV, "
              f"{len(p['live'])} live here")
        print(f"  people named   {len(emails):4d}")
        print(f"  resolved       {len(by_email):4d}")
        print(f"  unresolved     {len(p['unresolved']):4d}  "
              f"(no QueryHub account; nothing is created for them)")
        print(f"  teams  +{len(p['add_teams'])} ~{len(p['rename'])} "
              f"-{len(p['drop_teams'])}")
        print(f"  members +{len(p['add_members'])} -{len(p['del_members'])} "
              f"lead~{len(p['lead_changes'])}")
        empty = [t for t in names
                 if not p["want"].get(t) and t not in p["live"]]
        if empty:
            verb = "created too" if a.keep_empty else "skipped"
            print(f"  {len(empty)} team(s) resolve to nobody here and are "
                  f"{verb}: {', '.join(sorted(empty)[:5])}"
                  f"{' …' if len(empty) > 5 else ''}")
        for name, n in p["pinned"]:
            print(f"  KEPT  '{name}' is gone from the CSV but {n} live role(s) "
                  f"are scoped to it — remove those first")

        if not a.apply:
            print("\ndry run — nothing written. Re-run with --apply.")
            cur.connection.rollback()
            return 0

        apply(cur, a.source, names, p)
        audit.log_in(cur, None, a.actor, a.actor, "teams_imported",
                     {"source": a.source, "csv": str(a.csv),
                      "teams_added": len(p["add_teams"]),
                      "teams_removed": len(p["drop_teams"]),
                      "members_added": len(p["add_members"]),
                      "members_removed": len(p["del_members"]),
                      "unresolved": len(p["unresolved"]),
                      "notified": bool(a.notify)})
        print("\napplied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
