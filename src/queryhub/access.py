"""The authorization resolver, on the nine-table model.

This answers the same questions `teams.py` answers, from `access_grant` and
`role_assignment` instead of `teams` / `team_target_grants` /
`user_target_grants` / `admins` / `auto_approve_grants`. Nothing calls it yet:
it exists so `scripts/access_snapshot.py --backend new` can be diffed against
the baseline captured before any of this was built, and the switch happens only
when that diff is empty.

Every rule below is the OLD behaviour, restated. Where the new schema could
express something more natural, the old meaning wins — a migration that
improves an authorization decision is a migration that changed one.

THE FIVE RULES THAT ARE EASY TO GET WRONG

1. A waiver is not a grant. `auto_approve = TRUE` waives the approval wait up
   to its tier; it grants no access. Three of the live waivers are fleet-wide
   reads held by people with no fleet-wide access, so counting them as grants
   would hand out the fleet. The allowed tier comes only from rows where
   `auto_approve` is FALSE, and the auto tier is capped at the allowed tier —
   which is what the old code achieves by checking access first and the waiver
   second.

2. An expired principal grant does not fall through to the team. Such a row is
   usually written to NARROW what a team allows, so falling through would make
   an expiry WIDEN access. If a principal has covering rows but all of them
   have lapsed, the answer is "no access", not "whatever the team gives".

3. A revoked one does fall through. That is the older behaviour, kept
   deliberately: some revocations were issued expecting the team grant to
   resume, and changing it belongs in its own change with its own argument.

4. Only a principal's GRANT rows suppress the team, not their waivers. A waiver
   is a principal row and principal rows win, so a waiver would otherwise
   delete the team access of everyone holding an auto-approve window.

5. `admin` sees disabled targets; a fleet-wide grant does not. The old `admins`
   row and the old `bypass_team_grants` flag both meant "reach everything", but
   only the first also meant "see the catalog". `bypass` became a fleet-wide
   grant, so that distinction has to be re-established here rather than
   inherited from a column.

PRINCIPAL IDS. Callers hold Slack ids, and this module takes them: the
translation to `principal.id` happens inside the SQL, so it costs no extra
round trip. Two queries answer a submission, where the old resolver asked four.
"""
from __future__ import annotations

from . import db
from .targets import TargetServer

# One place that knows how a caller's id reaches a principal row. Widening this
# to other providers is a change here and nowhere else.
_SLACK = "slack"

_LIVE_ROLE = ("NOT ra.is_deleted AND ra.revoked_at IS NULL "
              "AND ra.valid_from <= NOW() "
              "AND (ra.valid_until IS NULL OR ra.valid_until > NOW())")

_ME = f"""
me AS (
    SELECT p.id
      FROM principal p
      JOIN principal_identity i ON i.principal_id = p.id
       AND NOT i.is_deleted AND i.provider = '{_SLACK}'
     WHERE i.external_id = %(pid)s AND NOT p.is_deleted
),
my_teams AS (
    SELECT tm.team_id FROM team_member tm
     WHERE tm.principal_id IN (SELECT id FROM me) AND NOT tm.is_deleted
)"""

_TARGET_COLUMNS = ("id, alias, host, port, default_database, username, enabled, "
                   "notes, COALESCE(engine, 'postgres') AS engine")


def _row_to_target(r) -> TargetServer:
    return TargetServer(
        id=r["id"], alias=r["alias"], host=r["host"], port=r["port"],
        default_database=r["default_database"], username=r["username"],
        enabled=r["enabled"], notes=r["notes"], engine=r["engine"])


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------


def roles(principal_id: str) -> list[dict]:
    """Every role in force for this principal, with its scope.

    **A disabled principal holds no roles.** The row survives — disabling is
    not revoking, and re-enabling restores the authority intact — but while
    the account is off it decides nothing, which is what the legacy
    `admins.is_admin` has always done (`WHERE ... AND enabled = TRUE`).

    Matching it here rather than in `_ME` is deliberate: the legacy GRANT
    resolvers do not filter on it, so pushing the predicate into the shared
    CTE would make the two models disagree in the other direction. The
    whitelist is enforced at entry (`requesters.is_allowed(uid) or
    admins.is_admin(uid)`, in eight places), and that second half is this
    function — so a role that outlives its account is not a stale row on a
    screen, it is a disabled person who can still open QueryHub.
    """
    return db.fetch_all(
        f"WITH {_ME} "
        "SELECT ra.role, ra.scope_team_id, ra.all_teams, ra.scope_target_id, "
        "       ra.all_targets, ra.max_tier, ra.any_tier, ra.valid_until "
        "  FROM role_assignment ra "
        "  JOIN principal p ON p.id = ra.principal_id AND p.enabled "
        " WHERE ra.principal_id IN (SELECT id FROM me) AND " + _LIVE_ROLE,
        {"pid": principal_id})


def is_admin(principal_id: str) -> bool:
    return any(r["role"] == "admin" for r in roles(principal_id))


def is_super_admin(principal_id: str) -> bool:
    """Unscoped, uncapped and permanent.

    `valid_until IS NULL` is load-bearing. The old model kept temporary admins
    in a separate table and refused to treat one as a super-admin; here both
    live in `role_assignment`, so a query for "unscoped admin" would also match
    someone holding the role for an afternoon. That person could then write role
    rows and read the PII exemptions marked super-admin-only.
    """
    return any(r["role"] == "admin" and r["all_teams"] and r["all_targets"]
               and r["any_tier"] and r["valid_until"] is None
               for r in roles(principal_id))


# ---------------------------------------------------------------------------
# grants
# ---------------------------------------------------------------------------
#
# `covering` selects the rows whose scope contains the question, without
# filtering on time: `resolve` needs to tell an EXPIRED row from an ABSENT one
# (rule 2), which a WHERE clause would erase.

def _covering(principal_id: str, target_id: int, database_name: str | None):
    return db.fetch_all(
        f"WITH {_ME} "
        "SELECT (g.principal_id IS NOT NULL) AS mine, g.tier, t.rank, "
        "       g.auto_approve, g.merge_with_team, g.all_targets, "
        "       g.all_databases, g.database_name, g.db_role, "
        "       (g.valid_until IS NOT NULL AND g.valid_until <= NOW()) AS expired, "
        "       (g.valid_from > NOW()) AS not_started "
        "  FROM access_grant g "
        "  JOIN tier t ON t.name = g.tier "
        " WHERE NOT g.is_deleted AND g.revoked_at IS NULL "
        "   AND (g.all_targets OR g.target_id = %(tid)s) "
        # The cast is required, not cosmetic: a bare parameter compared only
        # to NULL leaves Postgres unable to infer its type.
        "   AND (%(db)s::text IS NULL OR g.all_databases "
        "        OR g.database_name = %(db)s::text) "
        "   AND (g.principal_id IN (SELECT id FROM me) "
        "        OR g.team_id IN (SELECT team_id FROM my_teams))",
        {"pid": principal_id, "tid": target_id, "db": database_name})


def _live(rows):
    return [r for r in rows if not r["expired"] and not r["not_started"]]


def _decide(rows) -> dict | None:
    """Turn covering rows into an answer, or None for no access.

    Shared by `resolve` and `resolve_target` so the five rules have one
    implementation. A second copy of an authorization rule is a second place
    for it to be wrong.
    """
    live = _live(rows)
    mine_grants = [r for r in live if r["mine"] and not r["auto_approve"]]

    if not mine_grants:
        # Rule 2: lapsed rather than absent means no access, not the team's.
        if any(r["mine"] and not r["auto_approve"] and r["expired"] for r in rows):
            return None

    # Rule 4: only the principal's own GRANT rows displace the team.
    suppress_team = any(not r["merge_with_team"] for r in mine_grants)
    pool = mine_grants if suppress_team else \
        mine_grants + [r for r in live if not r["mine"] and not r["auto_approve"]]
    if not pool:
        return None

    # Rule 1: waivers never enter the pool that decides the tier.
    waivers = [r for r in live
               if r["auto_approve"] and (r["mine"] or not suppress_team)]
    best = max(pool, key=lambda r: r["rank"])

    # A waiver is CAPPED by the access, not filtered by it. A waiver at rw over
    # ro access still waives the wait on the ro queries that access permits —
    # the old check asks whether the window's tier COVERS the tier the query
    # needs, and a wider window covers a narrower query. Discarding such a
    # waiver would make people wait where they do not wait today.
    auto_tier = None
    if waivers:
        top = max(waivers, key=lambda r: r["rank"])
        auto_tier = best["tier"] if top["rank"] >= best["rank"] else top["tier"]

    return {
        "tier": best["tier"],
        "auto_tier": auto_tier,
        "source": "principal" if best["mine"] else "team",
        # Rule 5's other half: what "reach everything" looks like without the
        # admin role. `visible_targets` and the snapshot both need to tell the
        # two apart, and only the grant knows it was fleet-wide.
        "unrestricted": bool(best["all_targets"] and best["all_databases"]),
        "db_role": best["db_role"],
    }


def resolve(principal_id: str, target_id: int,
            database_name: str) -> dict | None:
    """The tier authority: what this principal may do to THIS database.

    Per database, not per target. Aggregating across a target's databases is
    how a read grant on one database and a write grant on another combine into
    write on both — the bug `teams.effective_mode_for_database` was written to
    avoid, and which one row per database removes by construction.
    """
    if is_admin(principal_id):
        return {"tier": "ddl", "auto_tier": _admin_auto(principal_id, target_id,
                                                        database_name),
                "source": "admin", "unrestricted": True, "db_role": None}
    return _decide(_covering(principal_id, target_id, database_name))


def _admin_auto(principal_id: str, target_id: int,
                database_name: str | None) -> str | None:
    """An admin's waiver still waives the wait.

    The admin branch used to hardcode `auto_tier: None`, on the reasoning that
    an admin can reach everything so a grant adds nothing. True of the GRANT
    and false of the WAIVER: the two are different questions, and the old
    model answers the second from `auto_approve_grants` without caring whether
    the holder is an admin.

    Found by the pre-cutover diff the first time an admin was given a
    fleet-wide RO waiver — 235 answers where the old model said the wait was
    skipped and the new one said it was not. Nothing was wrong with either
    model's grants; the new one simply never asked about the window.

    No cap is applied. An admin's access is `ddl`, which is the top of the
    ladder, so a waiver at any tier is already at or below it.
    """
    rows = [r for r in _covering(principal_id, target_id, database_name)
            if r["auto_approve"] and not r["expired"] and not r["not_started"]]
    if not rows:
        return None
    return max(rows, key=lambda r: r["rank"])["tier"]


def resolve_target(principal_id: str, target_id: int) -> dict | None:
    """The target-level answer, including which databases are reachable.

    `databases` is None for "every database", mirroring the old
    `allowed_databases`. Do not read `tier` from here to authorize a query: it
    is the highest tier across the databases named, which is not the tier on
    any one of them.
    """
    if is_admin(principal_id):
        return {"tier": "ddl", "auto_tier": _admin_auto(principal_id, target_id, None),
                "source": "admin", "unrestricted": True, "db_role": None,
                "databases": None}
    rows = _covering(principal_id, target_id, None)
    out = _decide(rows)
    if out is None:
        return None

    live = _live(rows)
    mine = [r for r in live if r["mine"] and not r["auto_approve"]]
    pool = mine if any(not r["merge_with_team"] for r in mine) else \
        mine + [r for r in live if not r["mine"] and not r["auto_approve"]]
    names: set[str] = set()
    for r in pool:
        if r["all_databases"]:
            out["databases"] = None
            return out
        if r["database_name"]:
            names.add(r["database_name"])
    out["databases"] = names
    return out


def resolve_many(principal_id: str,
                 target_ids: list[int]) -> dict[int, dict | None]:
    """`resolve_target` for many targets, in a fixed number of queries.

    For the admin screen that asks about every target at once. Same rules, one
    implementation of them: the rows are grouped here and handed to `_decide`,
    rather than the decision being written a second time.
    """
    ids = list(dict.fromkeys(int(t) for t in target_ids))
    if not ids:
        return {}
    if is_admin(principal_id):
        return {tid: {"tier": "ddl", "auto_tier": None, "source": "admin",
                      "unrestricted": True, "db_role": None, "databases": None}
                for tid in ids}

    rows = db.fetch_all(
        f"WITH {_ME} "
        "SELECT ts.id AS target_id, (g.principal_id IS NOT NULL) AS mine, "
        "       g.tier, t.rank, g.auto_approve, g.merge_with_team, "
        "       g.all_targets, g.all_databases, g.database_name, g.db_role, "
        "       (g.valid_until IS NOT NULL AND g.valid_until <= NOW()) AS expired, "
        "       (g.valid_from > NOW()) AS not_started "
        "  FROM access_grant g "
        "  JOIN tier t ON t.name = g.tier "
        "  JOIN target_servers ts ON (g.all_targets OR g.target_id = ts.id) "
        " WHERE NOT g.is_deleted AND g.revoked_at IS NULL "
        "   AND ts.id = ANY(%(ids)s) "
        "   AND (g.principal_id IN (SELECT id FROM me) "
        "        OR g.team_id IN (SELECT team_id FROM my_teams))",
        {"pid": principal_id, "ids": ids})

    by_target: dict[int, list] = {tid: [] for tid in ids}
    for r in rows:
        by_target[r["target_id"]].append(r)

    out: dict[int, dict | None] = {}
    for tid in ids:
        group = by_target[tid]
        decided = _decide(group)
        if decided is None:
            out[tid] = None
            continue
        live = _live(group)
        mine = [r for r in live if r["mine"] and not r["auto_approve"]]
        pool = mine if any(not r["merge_with_team"] for r in mine) else \
            mine + [r for r in live if not r["mine"] and not r["auto_approve"]]
        names: set[str] = set()
        unrestricted_dbs = False
        for r in pool:
            if r["all_databases"]:
                unrestricted_dbs = True
                break
            if r["database_name"]:
                names.add(r["database_name"])
        decided["databases"] = None if unrestricted_dbs else names
        out[tid] = decided
    return out


def can_use_target(principal_id: str, target_id: int) -> bool:
    return resolve_target(principal_id, target_id) is not None


def can_use_database(principal_id: str, target_id: int,
                     database_name: str) -> bool:
    """Whether the target-level answer admits this database.

    Deliberately the target-level question, matching the old
    `teams.can_use_database`: it asks whether the name is in the reachable set,
    not what tier applies to it. `resolve` is the tier authority.
    """
    grant = resolve_target(principal_id, target_id)
    if grant is None:
        return False
    dbs = grant["databases"]
    return dbs is None or database_name in dbs


# ---------------------------------------------------------------------------
# visibility
# ---------------------------------------------------------------------------


def visible_targets(principal_id: str) -> list[TargetServer]:
    """The catalog this principal may pick from.

    Rule 5. An admin sees every row including disabled ones, so a DBA can still
    reach a target taken out of service. Everyone else sees enabled targets
    they hold a covering GRANT on — waivers excluded, since a waived wait on a
    target you cannot reach was never a reason to show it.
    """
    if is_admin(principal_id):
        return [_row_to_target(r) for r in db.fetch_all(
            f"SELECT {_TARGET_COLUMNS} FROM target_servers "
            " ORDER BY enabled DESC, alias")]

    return [_row_to_target(r) for r in db.fetch_all(
        f"WITH {_ME} "
        f"SELECT DISTINCT {_TARGET_COLUMNS} "
        "  FROM target_servers ts "
        " WHERE ts.enabled AND EXISTS ( "
        "   SELECT 1 FROM access_grant g "
        "    WHERE NOT g.is_deleted AND g.revoked_at IS NULL "
        "      AND NOT g.auto_approve "
        "      AND g.valid_from <= NOW() "
        "      AND (g.valid_until IS NULL OR g.valid_until > NOW()) "
        "      AND (g.all_targets OR g.target_id = ts.id) "
        "      AND (g.principal_id IN (SELECT id FROM me) "
        "           OR g.team_id IN (SELECT team_id FROM my_teams))) "
        " ORDER BY ts.alias",
        {"pid": principal_id})]


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------

_TIER_RANK = {"ro": 0, "rw": 1, "ddl": 2}


def can_approve(principal_id: str, request: dict) -> bool:
    """Whether this principal may approve this request.

    `admin` approves fleet-wide up to its ceiling; `approver` approves within
    its scope. One in-scope row is enough — an out-of-scope row is not a veto,
    only the absence of any in-scope row is.

    The tier comes from the request's persisted `required_tier` where there is
    one, exactly as the old check does: it was classified with the target's
    engine at submit time and a client cannot tamper with it.
    """
    if not request:
        return False
    rows = [r for r in roles(principal_id) if r["role"] in ("admin", "approver")]
    if not rows:
        return False

    # Nobody reviews their own request — except an admin, who is fleet-wide and
    # could grant themselves the access anyway, so refusing them would remove a
    # workflow (an operator approving their own test submission) while
    # preventing nothing.
    #
    # For a SCOPED approver it prevents a great deal. A team lead who may
    # approve their own team up to RW is exactly the person who could otherwise
    # write the query and wave it through, which is the whole of the review
    # gone. The old model had no scoped approvers, so this rule is new because
    # the thing it guards is new.
    if request.get("requester_slack_id") == principal_id and \
            not any(r["role"] == "admin" for r in rows):
        return False

    tier = (request.get("required_tier") or "").strip().lower()
    tid = request.get("target_server_id")
    requester = request.get("requester_slack_id")
    requester_teams: set[int] | None = None

    for r in rows:
        if not r["any_tier"] and r["max_tier"]:
            if _TIER_RANK.get(tier, 99) > _TIER_RANK.get(r["max_tier"], 99):
                continue
        if not r["all_targets"]:
            if tid is None or r["scope_target_id"] != tid:
                continue
        if not r["all_teams"]:
            if not requester:
                continue
            if requester_teams is None:
                requester_teams = {
                    x["team_id"] for x in db.fetch_all(
                        f"WITH {_ME} "
                        "SELECT tm.team_id FROM team_member tm "
                        " WHERE tm.principal_id IN (SELECT id FROM me) "
                        "   AND NOT tm.is_deleted",
                        {"pid": requester})
                }
            if r["scope_team_id"] not in requester_teams:
                continue
        return True
    return False


# ---------------------------------------------------------------------------
# per-principal settings
# ---------------------------------------------------------------------------


def setting(principal_id: str, key: str) -> str | None:
    """A per-principal dial, or None. Expired settings read as absent."""
    row = db.fetch_one(
        f"WITH {_ME} "
        "SELECT s.setting_value FROM principal_setting s "
        " WHERE s.principal_id IN (SELECT id FROM me) "
        "   AND s.setting_key = %(key)s AND NOT s.is_deleted "
        "   AND (s.valid_until IS NULL OR s.valid_until > NOW()) ",
        {"pid": principal_id, "key": key})
    return row["setting_value"] if row else None


def has_fleet_wide_grant(principal_id: str) -> bool:
    """The successor to `bypass_team_grants`: reaches everything, but is not an
    admin and so does not see the disabled half of the catalog."""
    row = db.fetch_one(
        f"WITH {_ME} "
        "SELECT 1 FROM access_grant g "
        " WHERE g.principal_id IN (SELECT id FROM me) "
        "   AND NOT g.is_deleted AND g.revoked_at IS NULL AND NOT g.auto_approve "
        "   AND g.all_targets AND g.all_databases "
        "   AND g.valid_from <= NOW() "
        "   AND (g.valid_until IS NULL OR g.valid_until > NOW()) LIMIT 1",
        {"pid": principal_id})
    return row is not None

def search_visible_targets(principal_id: str, prefix: str,
                           limit: int = 100) -> list[TargetServer]:
    """`visible_targets` with an alias filter, for the typeahead.

    A separate query rather than filtering the full list in Python: the admin
    case is every row in the catalog, and paying for 112 of them on every
    keystroke is what the LIMIT is there to avoid.
    """
    like = f"%{prefix}%"
    if is_admin(principal_id):
        return [_row_to_target(r) for r in db.fetch_all(
            f"SELECT {_TARGET_COLUMNS} FROM target_servers "
            " WHERE alias ILIKE %(like)s ORDER BY enabled DESC, alias LIMIT %(lim)s",
            {"like": like, "lim": limit})]

    return [_row_to_target(r) for r in db.fetch_all(
        f"WITH {_ME} "
        f"SELECT DISTINCT {_TARGET_COLUMNS} "
        "  FROM target_servers ts "
        " WHERE ts.enabled AND ts.alias ILIKE %(like)s AND EXISTS ( "
        "   SELECT 1 FROM access_grant g "
        "    WHERE NOT g.is_deleted AND g.revoked_at IS NULL "
        "      AND NOT g.auto_approve "
        "      AND g.valid_from <= NOW() "
        "      AND (g.valid_until IS NULL OR g.valid_until > NOW()) "
        "      AND (g.all_targets OR g.target_id = ts.id) "
        "      AND (g.principal_id IN (SELECT id FROM me) "
        "           OR g.team_id IN (SELECT team_id FROM my_teams))) "
        " ORDER BY ts.alias LIMIT %(lim)s",
        {"pid": principal_id, "like": like, "lim": limit})]


def has_any_grant(principal_id: str) -> bool:
    """Anything at all, anywhere. `/sql` asks this to decide whether to offer
    the access-request flow instead of a target picker."""
    if is_admin(principal_id):
        return True
    row = db.fetch_one(
        f"WITH {_ME} "
        "SELECT 1 FROM access_grant g "
        " WHERE NOT g.is_deleted AND g.revoked_at IS NULL AND NOT g.auto_approve "
        "   AND g.valid_from <= NOW() "
        "   AND (g.valid_until IS NULL OR g.valid_until > NOW()) "
        "   AND (g.principal_id IN (SELECT id FROM me) "
        "        OR g.team_id IN (SELECT team_id FROM my_teams)) LIMIT 1",
        {"pid": principal_id})
    return row is not None


def expired_grant_at(principal_id: str, target_id: int):
    """When this principal's access here lapsed, if it did.

    "No access" reads the same to the person refused whether they never had it
    or had it until Thursday, and the second is the whole point of letting a
    grant expire. The latest expiry is the one they were relying on.
    """
    row = db.fetch_one(
        f"WITH {_ME} "
        "SELECT max(g.valid_until) AS at FROM access_grant g "
        " WHERE NOT g.is_deleted AND g.revoked_at IS NULL AND NOT g.auto_approve "
        "   AND g.valid_until IS NOT NULL AND g.valid_until <= NOW() "
        "   AND (g.all_targets OR g.target_id = %(tid)s) "
        "   AND (g.principal_id IN (SELECT id FROM me) "
        "        OR g.team_id IN (SELECT team_id FROM my_teams))",
        {"pid": principal_id, "tid": target_id})
    return (row or {}).get("at")


# ---------------------------------------------------------------------------
# the compatibility surface
# ---------------------------------------------------------------------------
#
# `teams.py` keeps its signatures and delegates its bodies here, so the cutover
# changes no call site — there are about 140 of them across ten modules, and a
# rewrite of that size is a rewrite with a missed one in it. The translation
# between the two vocabularies therefore lives here, in one place, rather than
# once per caller.


def legacy_shape(resolved: dict | None) -> dict | None:
    """A `resolve_target` answer in the words `effective_grant_for_user` uses.

    `source` is the only real difference. The old resolver says
    `admin_or_bypass` for both an admins row and a bypass flag, having no
    reason to separate them; the new one does separate them, because only the
    first also sees disabled targets. Collapsing them back is what keeps every
    caller — and the screens that print this string — working unchanged.
    """
    if resolved is None:
        return None
    if resolved["source"] == "admin" or resolved["unrestricted"]:
        source = "admin_or_bypass"
    else:
        source = "user" if resolved["source"] == "principal" else "team"
    return {"mode": resolved["tier"],
            "allowed_databases": resolved.get("databases"),
            "source": source}


def list_admins() -> list[dict]:
    """Everyone who counts as an admin right now, in the old row shape.

    `source` is `permanent` when the role has no end date and `temp` when it
    does, which is the distinction the old two-table union carried. Several
    callers key on it — the DM fan-out labels a temporary admin, and the
    reconcile compares only the permanent ones — so it has to survive the move
    into one table.
    """
    return db.fetch_all(
        "SELECT DISTINCT ON (i.external_id) "
        "       i.external_id AS slack_user_id, p.display_name AS name, "
        "       CASE WHEN ra.valid_until IS NULL THEN 'permanent' "
        "            ELSE 'temp' END AS source, "
        "       ra.valid_until AS expires_at "
        "  FROM role_assignment ra "
        "  JOIN principal p ON p.id = ra.principal_id AND NOT p.is_deleted "
        "  JOIN principal_identity i ON i.principal_id = p.id "
        "   AND NOT i.is_deleted AND i.provider = %(prov)s "
        " WHERE ra.role = 'admin' AND p.enabled AND " + _LIVE_ROLE +
        # Permanent wins over temporary for the same person, matching the old
        # union's NOT EXISTS clause.
        " ORDER BY i.external_id, (ra.valid_until IS NOT NULL), ra.valid_until",
        {"prov": _SLACK})

def warn_if_access_model_v2() -> None:
    """Say loudly, at boot, that reads and writes are on different models.

    `bot_config.access_model_v2` switches READS to the nine-table model. Writes
    still go to the old tables and reach the new ones through the migration 109
    mirror, so the two halves depend on a second switch being on and on the
    projection being correct. A process that starts in that state says so where
    an operator tailing the log will see it, because the failure it guards
    against is silent: a grant that exists in one model and not the other.

    Remove this once the old tables are gone; it is scaffolding for one phase.
    """
    from . import config
    try:
        if (config.get_setting("access_model_v2", "off") or "").strip().lower() == "on":
            _log_v2_warning()
    except Exception:      # a config read must never stop the service booting
        pass


def _log_v2_warning() -> None:
    import logging
    logging.getLogger("queryhub").warning(
        "access_model_v2 is ON: authorization is READ from the new tables. "
        "Writes still go to the old ones and are projected across by the "
        "migration 109 mirror, so check bot_config.access_model_mirror is on "
        "and scripts/copy_access_model.py --verify is clean.")
