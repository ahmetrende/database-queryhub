"""Team leads approve the requests sent to the targets their team owns.

THE RULE: the lead approves requests TO their team's databases, whoever sends
them, up to a ceiling (default RO). The team owns the data, so its lead is the
one who knows what a query against it means.

Two tables that a person can see and edit hold the answer: `target_team` says
which team owns a target, and `team_member.is_lead` says who speaks for the
team. This module turns them into approver rows in `role_assignment`: one row
per (lead, owned target), for any requester. `scripts/sync_team_approvers.py`
runs it for the whole fleet. The Connections screen runs it for one target,
right after an owner change, so the change takes effect at once.

WHAT IT DOES NOT DO, each one deliberate:

* It never grants access. An approver role decides other people's requests.
  It reaches no database.
* It never touches a row it does not own. It updates or revokes only the rows
  that carry its `source`. It also skips a key that a row from another source
  already holds: `role_assignment_live_uq` allows one live row per key, so an
  insert there would fail the whole transaction.
* It never invents a lead. It reports and skips a team with no `is_lead`
  member, or a lead with no QueryHub account.
* It never widens the tier. `max_tier` is a ceiling.
"""
from __future__ import annotations

# The source that the approver rows carry. The screen and the script must own
# the SAME rows. A screen that wrote under another name could not revoke the
# script's row when an owner goes, and the unique index would refuse the
# screen's row while the script's row is live.
SOURCE = "pod-sync"
MAX_TIER = "ro"
TIERS = ("ro", "rw", "ddl")


def resolve(cur, target_id: int | None = None):
    """(wanted, label, notes) from the model, for one target or for all.

    A wanted row is (lead principal, None, target): each team that owns a
    target, paired with that team's lead. The middle slot is the team scope,
    and it is None because the row admits any requester.
    """
    notes: list[str] = []
    wanted: set[tuple[int, int | None, int]] = set()
    label: dict[tuple[int, int | None, int], str] = {}

    sql = ("SELECT tt.target_id, tt.team_id, ts.alias, t.display_name AS team_name "
           "  FROM target_team tt "
           "  JOIN team t ON t.id = tt.team_id AND NOT t.is_deleted "
           "  JOIN target_servers ts ON ts.id = tt.target_id AND ts.enabled ")
    args: list = []
    if target_id is not None:
        sql += " WHERE tt.target_id = %s "
        args.append(target_id)
    cur.execute(sql + " ORDER BY t.display_name, ts.alias", args)
    owned = cur.fetchall()
    if not owned and target_id is None:
        notes.append("no team owns any enabled target — fill target_team first")

    leads: dict[int, list[dict]] = {}
    for row in owned:
        if row["team_id"] not in leads:
            cur.execute(
                "SELECT m.principal_id, p.display_name, p.enabled "
                "  FROM team_member m JOIN principal p ON p.id = m.principal_id "
                " WHERE m.team_id = %s AND m.is_lead "
                "   AND NOT m.is_deleted AND NOT p.is_deleted", (row["team_id"],))
            leads[row["team_id"]] = cur.fetchall()
            if not leads[row["team_id"]]:
                notes.append(f"'{row['team_name']}' owns targets but has no "
                             f"lead in QueryHub — skipped")
        for lead in leads[row["team_id"]]:
            if not lead["enabled"]:
                note = (f"'{row['team_name']}' lead {lead['display_name']} is "
                        f"disabled — skipped")
                if note not in notes:
                    notes.append(note)
                continue
            # No team in the key: the row admits any requester. Two teams
            # owning one target under the same lead are therefore one row.
            key = (lead["principal_id"], None, row["target_id"])
            wanted.add(key)
            label.setdefault(key, f"{lead['display_name']} ({row['team_name']})"
                                  f" @ {row['alias']}, any requester")
    return wanted, label, notes


def ceiling(cur, source: str = SOURCE) -> str:
    """The ceiling that the live rows of `source` carry, or MAX_TIER.

    The script takes `--max-tier`. The screen has no such flag, so it follows
    the last fleet run. Otherwise one owner change could re-tier a target's
    lead to a ceiling that the rest of the fleet does not have.
    """
    cur.execute("SELECT max_tier, count(*) AS n FROM role_assignment "
                " WHERE source = %s AND role = 'approver' "
                "   AND revoked_at IS NULL AND NOT is_deleted "
                " GROUP BY max_tier ORDER BY n DESC, max_tier LIMIT 1", (source,))
    row = cur.fetchone()
    return row["max_tier"] if row and row["max_tier"] in TIERS else MAX_TIER


def _held_elsewhere(cur, source: str, keys) -> set:
    """The keys among `keys` that a live approver row from another source
    already holds. The unique index would refuse a second row for them."""
    if not keys:
        return set()
    cur.execute(
        "SELECT principal_id, scope_target_id FROM role_assignment "
        " WHERE role = 'approver' AND scope_team_id IS NULL "
        "   AND revoked_at IS NULL AND NOT is_deleted "
        "   AND source IS DISTINCT FROM %s "
        "   AND (principal_id, scope_target_id) IN "
        "       (SELECT * FROM unnest(%s::bigint[], %s::bigint[]))",
        (source, [k[0] for k in keys], [k[2] for k in keys]))
    held = {(r["principal_id"], r["scope_target_id"]) for r in cur.fetchall()}
    return {k for k in keys if (k[0], k[2]) in held}


def plan(cur, source: str, max_tier: str, target_id: int | None = None):
    """What to add, revoke and re-tier so the live rows match the model.

    With `target_id`, only that target's rows are read and planned. Nothing
    else in the fleet changes, even when other targets have drifted.
    """
    wanted, label, notes = resolve(cur, target_id)
    sql = ("SELECT id, principal_id, scope_team_id, scope_target_id, max_tier "
           "  FROM role_assignment "
           " WHERE source = %s AND role = 'approver' "
           "   AND revoked_at IS NULL AND NOT is_deleted")
    args: list = [source]
    if target_id is not None:
        sql += " AND scope_target_id = %s"
        args.append(target_id)
    cur.execute(sql, args)
    live = {(r["principal_id"], r["scope_team_id"], r["scope_target_id"]): r
            for r in cur.fetchall()}

    add = sorted(wanted - set(live), key=lambda k: label.get(k, ""))
    held = _held_elsewhere(cur, source, add)
    for key in add:
        if key in held:
            notes.append(f"{label.get(key, key)} — already an approver through "
                         f"a row from another source, left as it is")
    add = [k for k in add if k not in held]
    drop = sorted(set(live) - wanted)
    # A ceiling that no longer matches is a revoke-and-recreate, because a role
    # is immutable and `role_assignment_live_uq` would refuse the pair anyway.
    retier = [k for k in wanted & set(live) if live[k]["max_tier"] != max_tier]
    return {"add": add, "drop": drop, "retier": retier,
            "live": live, "label": label, "notes": notes}


def apply(cur, source: str, p, max_tier: str, actor: str) -> None:
    """Write the plan. `actor` is the Slack id of whoever asked, or empty."""
    for key in p["drop"] + p["retier"]:
        cur.execute("UPDATE role_assignment SET revoked_at = NOW(), "
                    "       revoked_by = (SELECT p.id FROM principal p "
                    "         JOIN principal_identity i ON i.principal_id = p.id "
                    "        WHERE i.provider = 'slack' AND i.external_id = %s "
                    "          AND NOT i.is_deleted LIMIT 1) "
                    " WHERE id = %s", (actor, p["live"][key]["id"]))
    for key in p["add"] + p["retier"]:
        pid, team_id, target_id = key
        cur.execute(
            "INSERT INTO role_assignment "
            "  (principal_id, role, scope_team_id, all_teams, scope_target_id, "
            "   all_targets, max_tier, any_tier, reason, source, created_by) "
            "VALUES (%s,'approver',%s,%s,%s,FALSE,%s,FALSE,%s,%s,"
            "        (SELECT p.id FROM principal p "
            "           JOIN principal_identity i ON i.principal_id = p.id "
            "          WHERE i.provider='slack' AND i.external_id = %s "
            "            AND NOT i.is_deleted LIMIT 1))",
            (pid, team_id, team_id is None, target_id, max_tier,
             f"team lead, synced from {source}", source, actor))


def names(cur, keys) -> list[str]:
    """Display names for the principals in `keys`, in key order."""
    pids = [k[0] for k in keys]
    if not pids:
        return []
    cur.execute("SELECT id, display_name FROM principal WHERE id = ANY(%s)",
                (pids,))
    by_id = {r["id"]: r["display_name"] for r in cur.fetchall()}
    return [by_id.get(pid, str(pid)) for pid in pids]
