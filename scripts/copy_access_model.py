#!/usr/bin/env python3
"""Transcribe the old authorization tables into the new ones.

    python3 scripts/copy_access_model.py --dry-run     # what would change
    python3 scripts/copy_access_model.py               # write it
    python3 scripts/copy_access_model.py --verify      # read back and check

`--verify` is the gate on `bot_config.access_model_v2`. Reads follow that key;
every WRITE path still targets the old tables, so the new side goes stale the
moment anybody grants anything. Copy, verify, then flip — and flip back before
editing access, until the write paths move too.

Re-runnable, and that is the property everything else rests on. Every row is
matched to its source by a natural key, so a second run writes nothing: run it,
let a week of grants accumulate in the old tables, run it again, and the new
tables catch up rather than double. That matters because the copy happens well
before the cutover and the old tables stay live in between — the last run is the
one taken minutes before the switch.

Nothing here reads the new tables at runtime. Until `access.resolve` is wired
in, this only fills tables nobody consults.

WHAT MAPS TO WHAT

  requesters              -> principal + principal_identity(provider='slack')
  admins                  -> role_assignment(role='admin'), fleet-wide, and
                             max_tier carried over as the APPROVAL ceiling
  admins.can_grant        -> role_assignment(role='granter')
  bypass_team_grants      -> access_grant(all_targets, all_databases, ddl)
  teams / team_members    -> team(source='manual') / team_member
  team_target_grants      -> access_grant(team_id=…), one row per database
  user_target_grants      -> access_grant(principal_id=…, merge_with_team=false)
  auto_approve_grants     -> access_grant(auto_approve=true)
  user_row_limit_overrides-> principal_setting('max_rows')
  report_excluded_users   -> principal_setting('exclude_from_metrics')

TWO PLACES A NAIVE COPY WOULD CHANGE BEHAVIOUR, both measured, both handled:

  * An `auto_approve` row waives the wait, it does not grant the access
    (migration 106). Four of the thirty-two live windows sit where their holder
    has no matching access — three fleet-wide `ro` windows and one on an
    unreachable target — so counting them as grants would hand out fleet-wide
    read. They are copied as waivers and the resolver caps them at the access
    the principal actually has.
  * An `admins` row means access to EVERY target including disabled ones, which
    `bypass_team_grants` does not. Both admins here are also bypass rows; the
    admin role is what carries the wider visibility, and the copy writes both
    facts rather than collapsing them.

A grant whose `granted_by` names nobody in `requesters` or `admins` keeps the
grant and drops the attribution: two such values exist, and losing a grant to
preserve a name would be the wrong trade.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SLACK = "slack"


# ---------------------------------------------------------------------------
# planning: read both sides, decide the difference
# ---------------------------------------------------------------------------


def _principal_ids(cur) -> dict[str, int]:
    """slack id -> principal.id, for the identities this copy created."""
    cur.execute(
        "SELECT i.external_id, i.principal_id FROM principal_identity i "
        " WHERE i.provider = %s AND NOT i.is_deleted", (SLACK,))
    return {r["external_id"]: r["principal_id"] for r in cur.fetchall()}


def _team_ids(cur) -> dict[str, int]:
    cur.execute("SELECT name, id FROM team WHERE NOT is_deleted")
    return {r["name"]: r["id"] for r in cur.fetchall()}


def copy_principals(cur, apply: bool) -> dict:
    """One principal per Slack id, from requesters and any admin-only row.

    `enabled` is carried across as it stands. The new column defaults to FALSE
    because a directory sync must not be able to switch anybody on, but a
    transcription of an existing account is not a sync — refusing to carry
    `enabled` here would lock 29 people out at the cutover.
    """
    cur.execute(
        "SELECT slack_user_id AS sid, name, email, tz, enabled FROM requesters "
        " UNION ALL "
        "SELECT slack_user_id, name, email, tz, enabled FROM admins "
        " WHERE slack_user_id NOT IN (SELECT slack_user_id FROM requesters)")
    want = {r["sid"]: r for r in cur.fetchall()}
    have = _principal_ids(cur)
    created, updated = [], []

    for sid, r in sorted(want.items()):
        if sid in have:
            cur.execute(
                "SELECT display_name, email, tz, enabled FROM principal WHERE id = %s",
                (have[sid],))
            cur_row = cur.fetchone()
            if (cur_row["display_name"], cur_row["email"], cur_row["tz"],
                    cur_row["enabled"]) != (r["name"], r["email"], r["tz"], r["enabled"]):
                updated.append(sid)
                if apply:
                    cur.execute(
                        "UPDATE principal SET display_name=%s, email=%s, tz=%s, "
                        "enabled=%s WHERE id=%s",
                        (r["name"], r["email"], r["tz"], r["enabled"], have[sid]))
            continue
        created.append(sid)
        if apply:
            cur.execute(
                "INSERT INTO principal (kind, display_name, email, tz, enabled) "
                "VALUES ('person', %s, %s, %s, %s) RETURNING id",
                (r["name"], r["email"], r["tz"], r["enabled"]))
            pid = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO principal_identity (principal_id, provider, external_id) "
                "VALUES (%s, %s, %s)", (pid, SLACK, sid))
    return {"principal_created": len(created), "principal_updated": len(updated)}


def copy_teams(cur, apply: bool) -> dict:
    cur.execute("SELECT id, name, description FROM teams ORDER BY id")
    src = cur.fetchall()
    have = _team_ids(cur)
    created = [t["name"] for t in src if t["name"] not in have]
    if apply:
        for t in src:
            if t["name"] in have:
                continue
            cur.execute(
                "INSERT INTO team (name, display_name, source, external_id, attributes) "
                "VALUES (%s, %s, 'manual', %s, '{}'::jsonb)",
                (t["name"], t["name"], str(t["id"])))

    members = 0
    if apply or True:
        team_ids = _team_ids(cur) if apply else have
        people = _principal_ids(cur)
        cur.execute("SELECT tm.team_id, t.name, tm.slack_user_id AS sid "
                    "  FROM team_members tm JOIN teams t ON t.id = tm.team_id")
        for m in cur.fetchall():
            tid, pid = team_ids.get(m["name"]), people.get(m["sid"])
            if tid is None or pid is None:      # dry run before the team exists
                members += 1
                continue
            cur.execute("SELECT 1 FROM team_member WHERE team_id=%s AND principal_id=%s "
                        "  AND NOT is_deleted", (tid, pid))
            if cur.fetchone():
                continue
            members += 1
            if apply:
                cur.execute("INSERT INTO team_member (team_id, principal_id) "
                            "VALUES (%s, %s)", (tid, pid))
    return {"team_created": len(created), "team_member_created": members}


_SCOPE = (" WHERE principal_id  IS NOT DISTINCT FROM %(principal_id)s "
          "   AND team_id       IS NOT DISTINCT FROM %(team_id)s "
          "   AND target_id     IS NOT DISTINCT FROM %(target_id)s "
          "   AND database_name IS NOT DISTINCT FROM %(database_name)s "
          "   AND tier = %(tier)s AND auto_approve = %(auto_approve)s "
          "   AND NOT is_deleted ")


def _grant_exists(cur, valid_from=None, revoked_at=None, **k) -> bool:
    """Has this row already been copied?

    A live grant is identified by its scope alone, because that is exactly what
    the unique index enforces: "already copied" and "would collide" are one
    question.

    A revoked grant is not covered by that index — several revocations of the
    same access are legitimate history — so it needs a wider key, and the times
    are what separate them. Without this the three revoked user grants were
    re-inserted on every run: the live-only lookup could never find them, so
    each pass added three more rows. Idempotency is the whole contract here, and
    it fails first on the rows nobody looks at.
    """
    if revoked_at is None:
        cur.execute("SELECT 1 FROM access_grant" + _SCOPE + " AND revoked_at IS NULL", k)
    else:
        cur.execute(
            "SELECT 1 FROM access_grant" + _SCOPE +
            "   AND revoked_at = %(revoked_at)s "
            "   AND valid_from IS NOT DISTINCT FROM %(valid_from)s",
            {**k, "revoked_at": revoked_at, "valid_from": valid_from})
    return cur.fetchone() is not None


def _insert_grant(cur, *, principal_id=None, team_id=None, target_id=None,
                  database_name=None, tier="ro", auto_approve=False,
                  merge_with_team=False, db_role=None, valid_from=None,
                  valid_until=None, revoked_at=None, reason=None,
                  created_by=None) -> None:
    cur.execute(
        "INSERT INTO access_grant "
        "(principal_id, team_id, target_id, all_targets, database_name, "
        " all_databases, tier, auto_approve, merge_with_team, db_role, "
        " valid_from, valid_until, revoked_at, reason, created_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, now()),%s,%s,%s,%s)",
        (principal_id, team_id, target_id, target_id is None, database_name,
         database_name is None, tier, auto_approve, merge_with_team, db_role,
         valid_from, valid_until, revoked_at, reason, created_by))


def copy_grants(cur, apply: bool) -> dict:
    people, teams_ = _principal_ids(cur), _team_ids(cur)
    out = {"team_grant": 0, "user_grant": 0, "auto_window": 0, "bypass": 0,
           "unattributed": 0}

    def attribution(sid):
        if sid and sid not in people:
            out["unattributed"] += 1
        return people.get(sid)

    # bypass_team_grants: everything, everywhere, ddl
    cur.execute("SELECT slack_user_id AS sid FROM requesters WHERE bypass_team_grants")
    for r in cur.fetchall():
        pid = people.get(r["sid"])
        if pid is None:
            out["bypass"] += 1
            continue
        k = dict(principal_id=pid, team_id=None, target_id=None,
                 database_name=None, tier="ddl", auto_approve=False)
        if _grant_exists(cur, **k):
            continue
        out["bypass"] += 1
        if apply:
            _insert_grant(cur, reason="carried from bypass_team_grants", **k)

    # team_target_grants -> one row per database
    cur.execute(
        "SELECT g.team_id, t.name AS team_name, g.target_server_id AS tid, "
        "       g.allowed_databases AS dbs, g.mode, g.target_role, g.granted_at, "
        "       g.expires_at, g.revoked_at "
        "  FROM team_target_grants g JOIN teams t ON t.id = g.team_id")
    for g in cur.fetchall():
        tid = teams_.get(g["team_name"])
        for dbname in (g["dbs"] or [None]):
            k = dict(principal_id=None, team_id=tid, target_id=g["tid"],
                     database_name=dbname, tier=g["mode"], auto_approve=False)
            if tid is not None and _grant_exists(
                    cur, valid_from=g["granted_at"],
                    revoked_at=g["revoked_at"], **k):
                continue
            out["team_grant"] += 1
            if apply and tid is not None:
                _insert_grant(cur, db_role=g["target_role"],
                              valid_from=g["granted_at"], valid_until=g["expires_at"],
                              revoked_at=g["revoked_at"],
                              reason="carried from team_target_grants", **k)

    # user_target_grants -> merge_with_team FALSE, which is what "the user row
    # replaces the team's" means today
    cur.execute(
        "SELECT slack_user_id AS sid, target_server_id AS tid, allowed_databases AS dbs, "
        "       mode, granted_at, granted_by, expires_at, revoked_at "
        "  FROM user_target_grants")
    for g in cur.fetchall():
        pid = people.get(g["sid"])
        for dbname in (g["dbs"] or [None]):
            k = dict(principal_id=pid, team_id=None, target_id=g["tid"],
                     database_name=dbname, tier=g["mode"], auto_approve=False)
            if pid is not None and _grant_exists(
                    cur, valid_from=g["granted_at"],
                    revoked_at=g["revoked_at"], **k):
                continue
            out["user_grant"] += 1
            if apply and pid is not None:
                _insert_grant(cur, valid_from=g["granted_at"],
                              valid_until=g["expires_at"], revoked_at=g["revoked_at"],
                              created_by=attribution(g["granted_by"]),
                              reason="carried from user_target_grants", **k)

    # auto_approve_grants -> waivers, not grants (migration 106), and only the
    # ones still in force.
    #
    # 48 of the 80 rows have already expired, and an expired waiver is a record
    # of a permission that no longer applies — usually one of a series of
    # one-hour windows the same person was given on the same database. Their
    # scopes are identical, so the live-grant unique index admits exactly one of
    # each series and the rest were silently dropped: 80 rows in, 51 rows out.
    # Copying only the live ones is both honest and consistent — the expired
    # rows stay in auto_approve_grants, which is not dropped until phase 4, and
    # the audit trail has them either way.
    cur.execute(
        "SELECT slack_user_id AS sid, max_tier, target_server_id AS tid, "
        "       database_name AS dbn, starts_at, expires_at, reason, granted_by "
        "  FROM auto_approve_grants "
        " WHERE expires_at IS NULL OR expires_at > now() "
        " ORDER BY expires_at DESC NULLS FIRST")
    for g in cur.fetchall():
        pid = people.get(g["sid"])
        k = dict(principal_id=pid, team_id=None, target_id=g["tid"],
                 database_name=g["dbn"], tier=g["max_tier"], auto_approve=True)
        if pid is not None and _grant_exists(cur, **k):
            continue
        out["auto_window"] += 1
        if apply and pid is not None:
            _insert_grant(cur, merge_with_team=True, valid_from=g["starts_at"],
                          valid_until=g["expires_at"],
                          created_by=attribution(g["granted_by"]),
                          reason=g["reason"] or "carried from auto_approve_grants", **k)
    return out


def copy_roles(cur, apply: bool) -> dict:
    people = _principal_ids(cur)
    out = {"admin": 0, "granter": 0}
    cur.execute("SELECT slack_user_id AS sid, max_tier, can_grant, enabled FROM admins")
    for a in cur.fetchall():
        pid = people.get(a["sid"])
        roles = [("admin", a["max_tier"])] + ([("granter", None)] if a["can_grant"] else [])
        for role, max_tier in roles:
            if pid is not None:
                cur.execute(
                    "SELECT 1 FROM role_assignment WHERE principal_id=%s AND role=%s "
                    "  AND all_teams AND all_targets AND NOT is_deleted "
                    "  AND revoked_at IS NULL", (pid, role))
                if cur.fetchone():
                    continue
            out[role] += 1
            if apply and pid is not None:
                cur.execute(
                    "INSERT INTO role_assignment (principal_id, role, all_teams, "
                    " all_targets, max_tier, any_tier, reason) "
                    "VALUES (%s,%s,true,true,%s,%s,'carried from admins')",
                    (pid, role, max_tier, max_tier is None))
    return out


def copy_settings(cur, apply: bool) -> dict:
    people = _principal_ids(cur)
    out = {"setting": 0}
    rows = []
    cur.execute("SELECT slack_user_id AS sid, max_rows, expires_at, reason "
                "  FROM user_row_limit_overrides")
    rows += [(r["sid"], "max_rows", str(r["max_rows"]), r["expires_at"], r["reason"])
             for r in cur.fetchall()]
    cur.execute("SELECT slack_user_id AS sid, reason FROM report_excluded_users")
    rows += [(r["sid"], "exclude_from_metrics", "true", None, r["reason"])
             for r in cur.fetchall()]

    for sid, key, value, until, reason in rows:
        pid = people.get(sid)
        if pid is not None:
            cur.execute("SELECT 1 FROM principal_setting WHERE principal_id=%s "
                        "  AND setting_key=%s AND NOT is_deleted", (pid, key))
            if cur.fetchone():
                continue
        out["setting"] += 1
        if apply and pid is not None:
            cur.execute(
                "INSERT INTO principal_setting (principal_id, setting_key, "
                " setting_value, valid_until, reason) VALUES (%s,%s,%s,%s,%s)",
                (pid, key, value, until, reason))
    return out


# ---------------------------------------------------------------------------
# verify: the counts have to add up, independently of the copy that wrote them
# ---------------------------------------------------------------------------


def verify(cur) -> list[str]:
    problems = []

    def one(sql, args=()):
        cur.execute(sql, args)
        return list(cur.fetchone().values())[0]

    checks = [
        ("principal", "SELECT count(*) FROM principal",
         "SELECT count(DISTINCT sid) FROM ("
         "  SELECT slack_user_id sid FROM requesters UNION "
         "  SELECT slack_user_id FROM admins) x"),
        ("principal_identity", "SELECT count(*) FROM principal_identity", None),
        # Scoped to `source = 'manual'`, which is the half this script and the
        # migration-109 mirror own. `team` also carries structures imported
        # from outside (scripts/import_teams.py writes its own source), and
        # counting those against the legacy table made the gate cry wolf the
        # first time an org import ran — 19 vs 6, with nothing wrong.
        ("team", "SELECT count(*) FROM team "
                 " WHERE source = 'manual' AND NOT is_deleted",
         "SELECT count(*) FROM teams"),
        ("team_member", "SELECT count(*) FROM team_member m "
                        "  JOIN team t ON t.id = m.team_id "
                        " WHERE t.source = 'manual' "
                        "   AND NOT m.is_deleted AND NOT t.is_deleted",
         "SELECT count(*) FROM team_members"),
        # Mirror-owned rows only, LIVE on both sides. `access_grant` also holds
        # team grants that exist only in the new model -- every pod grant
        # written since the cutover -- and counting those against the legacy
        # table made the gate report `new 72 vs old 0` about a model that was
        # exactly right. Same fault as the `team` check above, one table over:
        # the marker for "this row projects a legacy row" is `mirrored_from`,
        # and a row without one was never the legacy table's to answer for.
        ("team grants",
         "SELECT count(*) FROM access_grant "
         " WHERE team_id IS NOT NULL "
         "   AND mirrored_from = 'team_target_grants' "
         "   AND revoked_at IS NULL AND NOT is_deleted "
         "   AND (valid_until IS NULL OR valid_until > now())",
         "SELECT sum(coalesce(cardinality(allowed_databases),1)) "
         "  FROM team_target_grants "
         " WHERE revoked_at IS NULL "
         "   AND (expires_at IS NULL OR expires_at > now())"),
        # Distinct SCOPES, not rows: overlapping waivers at the same tier on the
        # same database mean "waived until the later of the two", and the copy
        # keeps the widest. Five live rows here collapse to two scopes, both
        # belonging to one person who was granted the same window repeatedly.
        # LIVE on both sides. The new-side count used to include revoked rows,
        # so the first time waivers were narrowed — 14 of them, correctly
        # revoked by the mirror — the gate reported drift that did not exist
        # and would have stopped a cutover for nothing. A revoked waiver is
        # not a window; it is the record that one closed.
        ("auto windows (live scopes)",
         "SELECT count(*) FROM access_grant "
         " WHERE auto_approve AND revoked_at IS NULL AND NOT is_deleted "
         "   AND (valid_until IS NULL OR valid_until > now())",
         "SELECT count(*) FROM ("
         "  SELECT 1 FROM auto_approve_grants "
         "   WHERE expires_at IS NULL OR expires_at > now() "
         "   GROUP BY slack_user_id, target_server_id, database_name, max_tier) x"),
        ("settings", "SELECT count(*) FROM principal_setting",
         "SELECT (SELECT count(*) FROM user_row_limit_overrides) + "
         "       (SELECT count(*) FROM report_excluded_users)"),
        ("admin roles", "SELECT count(*) FROM role_assignment WHERE role='admin'",
         "SELECT count(*) FROM admins"),
    ]
    for label, got_sql, want_sql in checks:
        got = one(got_sql)
        if want_sql is None:
            print(f"  {label}: {got}")
            continue
        want = one(want_sql) or 0
        mark = "OK " if got == want else "!! "
        print(f"  {mark}{label}: new {got} vs old {want}")
        if got != want:
            problems.append(f"{label}: {got} != {want}")

    # user grants: the old rows explode per database, so compare the exploded
    # total -- both sides scoped to what the mirror owns and to rows that are
    # LIVE.
    #
    # This used to select on `reason = 'carried from user_target_grants'`, a
    # string this script writes and the mirror does not (it writes "mirrored
    # from ..."), and it filtered nothing. So a grant made after the cutover was
    # invisible to the gate while a revoked one still counted: 82 vs 80, drift
    # reported about two models that agreed row for row. `mirrored_from` is the
    # marker; the reason is prose, and prose is not a predicate. The drift check
    # below compares these same two sets element-wise and found no difference,
    # which is how the count was caught being wrong rather than the data.
    got = one("SELECT count(*) FROM access_grant "
              " WHERE principal_id IS NOT NULL AND NOT auto_approve "
              "   AND mirrored_from = 'user_target_grants' "
              "   AND revoked_at IS NULL AND NOT is_deleted "
              "   AND (valid_until IS NULL OR valid_until > now())")
    want = one("SELECT sum(coalesce(cardinality(allowed_databases),1)) "
               "  FROM user_target_grants "
               " WHERE revoked_at IS NULL "
               "   AND (expires_at IS NULL OR expires_at > now())") or 0
    mark = "OK " if got == want else "!! "
    print(f"  {mark}user grants: new {got} vs old {want}")
    if got != want:
        problems.append(f"user grants: {got} != {want}")

    orphan = one("SELECT count(*) FROM access_grant g WHERE g.principal_id IS NULL "
                 "  AND g.team_id IS NULL")
    if orphan:
        problems.append(f"{orphan} grants with no subject")

    # Drift — the gate on turning the flag on. Reads follow
    # `access_model_v2`, so if it is on while the new side is behind, the
    # grant somebody was just given does nothing and nobody can see why.
    #
    # This used to ask whether any legacy grant was TIMESTAMPED after the
    # newest row in `access_grant`, which was the right approximation while
    # the copy was the only thing writing the new tables. Migration 109 made
    # it wrong in both directions: the mirror propagates each legacy write
    # inside the same transaction, so `granted_at` and the mirrored
    # `created_at` are microseconds apart in an order nothing guarantees —
    # measured 2026-09-08, two grants flagged as drift with the rows already
    # present and identical.
    #
    # A clock cannot answer this question. Compare the SETS: every live legacy
    # grant, exploded per database, against every live mirrored row. That is
    # exact, says which side is missing what, and costs one query.
    drift = one(
        "WITH old AS ("
        "  SELECT u.slack_user_id sid, u.target_server_id tid, d.db dbn, u.mode m"
        "    FROM user_target_grants u"
        "    LEFT JOIN LATERAL unnest(COALESCE(u.allowed_databases,"
        "                                      ARRAY[NULL::text])) d(db) ON TRUE"
        "   WHERE u.revoked_at IS NULL"
        "     AND (u.expires_at IS NULL OR u.expires_at > now())), "
        "new AS ("
        "  SELECT i.external_id sid, g.target_id tid, g.database_name dbn, g.tier m"
        "    FROM access_grant g"
        "    JOIN principal_identity i ON i.principal_id = g.principal_id"
        "     AND i.provider = 'slack' AND NOT i.is_deleted"
        "   WHERE g.mirrored_from = 'user_target_grants'"
        "     AND g.revoked_at IS NULL AND NOT g.is_deleted AND NOT g.auto_approve"
        "     AND (g.valid_until IS NULL OR g.valid_until > now())) "
        "SELECT (SELECT count(*) FROM (SELECT * FROM old EXCEPT SELECT * FROM new) a)"
        "     + (SELECT count(*) FROM (SELECT * FROM new EXCEPT SELECT * FROM old) b)")
    if drift:
        problems.append(
            f"{drift} user grant(s) differ between the two models — the mirror "
            f"is off or behind. Re-run this script before turning "
            f"access_model_v2 on")
    return problems


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be written and roll back")
    ap.add_argument("--verify", action="store_true",
                    help="only count both sides and compare")
    ap.add_argument("--actor", default=None,
                    help="principal id to record as the actor on the audit row; "
                         "omitted, the script names itself")
    args = ap.parse_args(argv)

    from queryhub import audit, db

    if args.verify:
        with db.connection() as conn:
            problems = verify(conn.cursor())
        if problems:
            print("\nMISMATCH:")
            for p in problems:
                print(f"  {p}")
            return 1
        print("\nboth sides agree")
        return 0

    apply = not args.dry_run
    totals: dict = {}
    with db.transaction() as cur:
        # Migration 108 puts auth-event triggers on the new tables, so every
        # row this writes would DM its subject. A transcription is not a change
        # — the person already has the access, in the other model — and the
        # first run alone would send 152 messages announcing nothing. This is
        # the same GUC the app paths use when they send their own DM.
        cur.execute("SET LOCAL app.auth_dm_suppress = 'on'")
        for step in (copy_principals, copy_teams, copy_grants, copy_roles,
                     copy_settings):
            totals |= step(cur, apply)
        written = sum(v for k, v in totals.items() if k != "unattributed")
        if apply and written:
            # log_in, not log: `audit.log` opens its own connection, so on a
            # dry run its row would survive the rollback and claim a copy that
            # never happened.
            audit.log_in(cur, None, args.actor, args.actor or "copy_access_model",
                         "access_model_copied", totals)
        if args.dry_run:
            raise _Rollback(totals)

    _report(totals, apply)
    return 0


class _Rollback(Exception):
    """Ends the transaction without committing. `db.transaction()` commits on a
    clean exit, so a dry run has to leave by the other door."""


def _report(totals: dict, applied: bool) -> None:
    verb = "wrote" if applied else "would write"
    print(f"{verb}:")
    for k, v in sorted(totals.items()):
        if k == "unattributed":
            continue
        print(f"  {k:24s} {v}")
    if totals.get("unattributed"):
        print(f"  {'(granted_by unresolved)':24s} {totals['unattributed']} "
              f"— grant kept, attribution dropped")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except _Rollback as rolled_back:
        _report(rolled_back.args[0], applied=False)
        raise SystemExit(0) from None
