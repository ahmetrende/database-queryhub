#!/usr/bin/env python3
"""Freeze what every principal may do, so a change to the resolver has to prove
it changed nothing.

The access model is moving off `teams` / `team_target_grants` / `user_target_grants`
and onto a single grant table. That migration is only safe if the new resolver
answers every question exactly as the old one does, and "exactly" has to mean
every question, not a sample: the interesting cases are the rare ones — an
expired override that must NOT fall through, a scope written `{}` instead of
NULL, a database named in one team grant and not another.

So: capture the current answers into a file, migrate, capture again with the new
backend, and diff. A green diff is the evidence; nothing else is.

    python3 scripts/access_snapshot.py capture --out before.json
    …migrate…
    python3 scripts/access_snapshot.py capture --backend new --out after.json
    python3 scripts/access_snapshot.py compare before.json after.json

`compare` exits non-zero on the first difference, so it can gate a deploy.

What is captured, per (principal, target, database):

  tier        effective_mode_for_database — the tier AUTHORITY. Deliberately
              not effective_grant_for_user()["mode"], which returns the max
              tier across the UNION of databases and would read as RW on a
              database only a read grant covers.
  visible     is the target in the user's picker at all
  can_target  may they aim at this server
  can_db      may they reach this database
  source      which layer answered: user, team, admin_or_bypass, none
  auto        the highest tier auto-approved for this exact scope, if any

Plus, per principal: admin / super-admin / bypass flags and any row-limit
override. Plus an approver matrix: for each admin, which requesters' requests
they may approve, per target and tier.

Read-only. It opens the bot DB, calls the same functions a submission calls,
and writes a file.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _bot():
    """The bot package, imported on first use rather than at module scope.

    `compare` is the half of this tool that gates a deploy, and it reads two
    files. Importing the package here would have it open the bot DB — and
    `config.py` raises when the connection variables are absent — so the gate
    would refuse to run in CI, on a laptop, or anywhere the answer is already
    in the files. Capture needs the database; comparing two captures does not.
    """
    from queryhub import (admins, auto_approve, db, requesters,
                               row_limits, teams)
    return admins, auto_approve, db, requesters, row_limits, teams


TIERS = ("ro", "rw", "ddl")
_TIER_RANK = {name: i for i, name in enumerate(TIERS)}

# A database name that appears in no grant list anywhere. Its answers are what
# prove the wildcard semantics survived: a grant with an empty or NULL database
# list must cover it, a grant that names databases must not.
UNLISTED = "__unlisted_database__"


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------
#
# One object per implementation of the same questions. `legacy` is the resolver
# in `teams.py`; `new` is whatever replaces it, and is looked up lazily so this
# script keeps working before that module exists.


class LegacyBackend:
    name = "legacy"

    def __init__(self):
        (self.admins, self.auto_approve, self.db, self.requesters,
         self.row_limits, self.teams) = _bot()

    def tier(self, pid, tid, dbname):
        return self.teams.effective_mode_for_database(pid, tid, dbname)

    def grant_source(self, pid, tid):
        g = self.teams.effective_grant_for_user(pid, tid)
        return g["source"] if g else "none"

    def can_target(self, pid, tid):
        return self.teams.can_use_target(pid, tid)

    def can_database(self, pid, tid, dbname):
        return self.teams.can_use_database(pid, tid, dbname)

    def visible_targets(self, pid):
        return sorted(t.id for t in self.teams.list_targets_for_user(pid))

    def auto_tier(self, pid, tid, dbname):
        """The highest tier that would actually skip the wait here.

        Capped by the access, because that is the order a submission runs in:
        `core_submit` rejects on the tier check before it ever consults
        `auto_approve`, so a window on a database its holder cannot reach never
        fires. Reading the window table alone reported `ro` on 618 (principal,
        target, database) triples with no access at all — three people hold
        fleet-wide read windows — which measured the table rather than the
        behaviour, and would have shown as a regression in a resolver that got
        this right.
        """
        allowed = self.teams.effective_mode_for_database(pid, tid, dbname)
        if allowed is None:
            return None
        for mode in reversed(TIERS):
            if _TIER_RANK[mode] > _TIER_RANK[allowed]:
                continue
            if self.auto_approve.effective_grant(pid, mode, tid, dbname):
                return mode
        return None

    def flags(self, pid):
        return {
            "is_admin": self.admins.is_admin(pid),
            "is_super_admin": self.admins.is_super_admin(pid),
            "bypass": self.requesters.bypasses_team_grants(pid),
            "row_caps": list(self.row_limits.effective_caps(pid)),
        }

    def can_approve(self, admin_id, request):
        return self.admins.can_approve(admin_id, request)


class NewBackend(LegacyBackend):
    """The nine-table resolver, asked the legacy questions.

    Subclasses the legacy backend so anything not yet reimplemented fails loudly
    on the method rather than quietly reporting the old answer — a green diff
    that proves nothing is worse than a red one.

    Two answers are translated rather than delegated, because the vocabularies
    differ where the models do:

    `source`. The old resolver says `admin_or_bypass` for both an admins row and
    a bypass flag, having no reason to separate them. The new one distinguishes
    an `admin` role from a fleet-wide grant, because only the first also sees
    disabled targets. Mapping the second back onto the old word is a
    comparison concern and lives here, not in the resolver.

    `row_caps`. Row limits are not part of this rewrite: `row_limits` still
    reads `user_row_limit_overrides`, and `principal_setting` carries a copy
    that nothing consumes until phase 4. Delegated unchanged, so the diff makes
    no claim either way about a component that has no new implementation.
    """

    name = "new"

    def __init__(self):
        super().__init__()
        try:
            from queryhub import access
        except ImportError as exc:  # pragma: no cover - until the module lands
            raise SystemExit(
                "the new backend needs queryhub.access, which does not "
                "exist yet — capture with --backend legacy for now"
            ) from exc
        self.access = access

    def tier(self, pid, tid, dbname):
        got = self.access.resolve(pid, tid, dbname)
        return got["tier"] if got else None

    def grant_source(self, pid, tid):
        # Through the same adapter `teams.py` uses, so the comparison exercises
        # the translation the product will run rather than a second copy of it.
        shaped = self.access.legacy_shape(
            self.access.resolve_target(pid, tid))
        return shaped["source"] if shaped else "none"

    def can_target(self, pid, tid):
        return self.access.can_use_target(pid, tid)

    def can_database(self, pid, tid, dbname):
        return self.access.can_use_database(pid, tid, dbname)

    def visible_targets(self, pid):
        return sorted(t.id for t in self.access.visible_targets(pid))

    def auto_tier(self, pid, tid, dbname):
        got = self.access.resolve(pid, tid, dbname)
        return got["auto_tier"] if got else None

    def flags(self, pid):
        return {
            "is_admin": self.access.is_admin(pid),
            "is_super_admin": self.access.is_super_admin(pid),
            "bypass": self.access.has_fleet_wide_grant(pid),
            "row_caps": list(self.row_limits.effective_caps(pid)),
        }

    def can_approve(self, admin_id, request):
        return self.access.can_approve(admin_id, request)


BACKENDS = {"legacy": LegacyBackend, "new": NewBackend}


# --------------------------------------------------------------------------
# what to ask about
# --------------------------------------------------------------------------


def principals(db) -> list[dict]:
    """Everyone the resolver can be asked about: requesters and admins, whether
    enabled or not. A disabled row still has to resolve the same way after the
    migration as before it."""
    rows = db.fetch_all(
        "SELECT slack_user_id AS id, name, enabled, 'requester' AS kind "
        "  FROM requesters "
        " UNION "
        "SELECT slack_user_id, name, enabled, 'admin' "
        "  FROM admins "
        " WHERE slack_user_id NOT IN (SELECT slack_user_id FROM requesters)"
    )
    return sorted(({"id": r["id"], "name": r["name"], "enabled": r["enabled"],
                    "kind": r["kind"]} for r in rows), key=lambda r: r["id"])


def targets(db) -> list[dict]:
    rows = db.fetch_all(
        "SELECT id, alias, enabled, default_database FROM target_servers ORDER BY id"
    )
    return [dict(r) for r in rows]


def databases_by_target(db) -> dict[int, list[str]]:
    """Every database name any rule mentions for a target, plus its default and
    the unlisted sentinel. Asking about databases nobody named would measure
    nothing; these are the names where the answer can differ."""
    named: dict[int, set[str]] = {}
    for sql in (
        "SELECT target_server_id AS tid, unnest(allowed_databases) AS db "
        "  FROM team_target_grants WHERE allowed_databases IS NOT NULL",
        "SELECT target_server_id AS tid, unnest(allowed_databases) AS db "
        "  FROM user_target_grants WHERE allowed_databases IS NOT NULL",
        "SELECT target_server_id AS tid, database_name AS db "
        "  FROM auto_approve_grants WHERE database_name IS NOT NULL "
        "   AND target_server_id IS NOT NULL",
    ):
        for r in db.fetch_all(sql):
            named.setdefault(r["tid"], set()).add(r["db"])
    out: dict[int, list[str]] = {}
    for t in targets(db):
        names = set(named.get(t["id"], ()))
        if t["default_database"]:
            names.add(t["default_database"])
        names.add(UNLISTED)
        out[t["id"]] = sorted(names)
    return out


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------


def capture(backend, progress=True) -> dict:
    db = backend.db
    people, tgts, dbs = principals(db), targets(db), databases_by_target(db)
    started = time.monotonic()

    flags = {p["id"]: backend.flags(p["id"]) for p in people}
    visible = {p["id"]: backend.visible_targets(p["id"]) for p in people}

    access: dict[str, dict] = {}
    total = len(people)
    for n, p in enumerate(people, 1):
        pid = p["id"]
        seen = set(visible[pid])
        for t in tgts:
            tid = t["id"]
            can_t = backend.can_target(pid, tid)
            source = backend.grant_source(pid, tid)
            for dbname in dbs[tid]:
                access[f"{pid}|{tid}|{dbname}"] = {
                    "tier": backend.tier(pid, tid, dbname),
                    "visible": tid in seen,
                    "can_target": can_t,
                    "can_db": backend.can_database(pid, tid, dbname),
                    "source": source,
                    "auto": backend.auto_tier(pid, tid, dbname),
                }
        if progress:
            print(f"  [{n}/{total}] {pid} {p['name'] or ''}".rstrip(),
                  file=sys.stderr, flush=True)

    # The approver matrix. Only enabled targets: an approval decision on a
    # disabled target is not a thing anyone can make.
    admin_ids = sorted({a["slack_user_id"] for a in backend.admins.list_active()})
    approve: dict[str, bool] = {}
    for aid in admin_ids:
        for p in people:
            for t in tgts:
                if not t["enabled"]:
                    continue
                for tier in TIERS:
                    req = {"requester_slack_id": p["id"],
                           "target_server_id": t["id"],
                           "required_tier": tier,
                           "query": ""}
                    approve[f"{aid}|{p['id']}|{t['id']}|{tier}"] = \
                        backend.can_approve(aid, req)

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backend": backend.name,
        "counts": {"principals": len(people), "targets": len(tgts),
                   "access": len(access), "approve": len(approve),
                   "seconds": round(time.monotonic() - started, 1)},
        "flags": flags,
        "visible": visible,
        "access": access,
        "approve": approve,
    }


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------


def diff(before: dict, after: dict, limit: int = 40) -> list[str]:
    """Every way the two snapshots disagree, most alarming first.

    Order matters when the list is truncated: a key that vanished is a person
    who lost access, and that must be the first thing on screen — not the
    thirtieth line of tier changes.
    """
    out: list[str] = []
    for section in ("flags", "visible", "access", "approve"):
        b, a = before.get(section, {}), after.get(section, {})
        gone = sorted(set(b) - set(a))
        added = sorted(set(a) - set(b))
        for k in gone:
            out.append(f"{section}: {k} disappeared (was {b[k]!r})")
        for k in added:
            out.append(f"{section}: {k} appeared (now {a[k]!r})")
        for k in sorted(set(b) & set(a)):
            if b[k] != a[k]:
                out.append(f"{section}: {k}\n    before {b[k]!r}\n    after  {a[k]!r}")
    return out[:limit] if limit and len(out) > limit else out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="write a snapshot of today's answers")
    cap.add_argument("--backend", choices=sorted(BACKENDS), default="legacy")
    cap.add_argument("--out", required=True, type=Path)
    cap.add_argument("--quiet", action="store_true")

    cmp_ = sub.add_parser("compare", help="diff two snapshots; non-zero if they differ")
    cmp_.add_argument("before", type=Path)
    cmp_.add_argument("after", type=Path)
    cmp_.add_argument("--limit", type=int, default=40,
                      help="differences to print (0 = all)")

    args = ap.parse_args(argv)

    if args.cmd == "capture":
        snap = capture(BACKENDS[args.backend](), progress=not args.quiet)
        args.out.write_text(json.dumps(snap, indent=1, sort_keys=True,
                                       default=str), encoding="utf-8")
        c = snap["counts"]
        print(f"{args.out}: {c['access']} access answers, {c['approve']} approval "
              f"answers, {c['principals']} principals, {c['targets']} targets, "
              f"{c['seconds']}s")
        return 0

    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    diffs = diff(before, after, args.limit)
    if not diffs:
        print(f"identical: {before['backend']} and {after['backend']} agree on "
              f"{len(before['access'])} access and {len(before['approve'])} "
              f"approval answers")
        return 0
    total = len(diff(before, after, 0))
    print(f"{total} difference(s):")
    for line in diffs:
        print(f"  {line}")
    if total > len(diffs):
        print(f"  … and {total - len(diffs)} more (pass --limit 0)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
