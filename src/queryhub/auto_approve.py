"""Auto-approval grants — time-bounded, tier-scoped exemption from
the admin approval gate.

A grant matches a request when:
  - NOW() (or a caller-supplied moment) is inside [starts_at, expires_at)
  - grant.max_tier >= required_mode

If multiple grants for the same user match, the most permissive
(highest max_tier, then latest expires_at) wins. For "is this grant
still valid at the scheduled run time?" — the submit handler calls
decision(at_time=scheduled_for) and falls back to manual approval if it
returns None.

Whether a request may skip review is asked of ONE function, `decision`, by
everything that decides it and everything that announces it. It asks
`effective_grant` for the best waiver that may decide on the target
(`waiver_applies`), and on a target under the archive rule it also lets an
admin or the owning team's lead through, waiver or not. A test reads the
package and fails any other caller of `effective_grant`.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from . import config as cfg
from . import db, engines

# Tier ordering — ddl is the most permissive.
_TIER_RANK = {"ro": 0, "rw": 1, "ddl": 2}


def _covers(max_tier: str, required_mode: str) -> bool:
    return _TIER_RANK[max_tier] >= _TIER_RANK[required_mode]


class ScopeError(ValueError):
    """A database scope that could never match a request."""


# What a caller might type meaning "every database on this target". The model
# spells that as NULL, and `grant_covers` compares a non-NULL scope for
# EQUALITY — so any of these stored literally is a grant that matches nothing.
_ANY_DATABASE = {"", "*", "all", "any"}


def normalise_scope(database_name: str | None) -> str | None:
    """Fold the "every database" spellings down to NULL.

    The web admin form defaults its database field to `*` and posted it
    verbatim, which produced grants that silently never fired: the request
    dropped to manual approval exactly as if no grant existed, with an active
    grant sitting in the table. Nothing logged, because nothing failed.
    """
    if database_name is None:
        return None
    name = database_name.strip()
    return None if name.lower() in _ANY_DATABASE else name


def validate_scope(target_server_id: int | None,
                   database_name: str | None) -> None:
    """Refuse a database that does not exist on the target.

    The other half of the same failure: a typo is indistinguishable from a
    correct name until someone notices approvals still arriving by hand. Only
    checked against a target whose catalog has actually been snapshotted —
    a freshly onboarded server has no rows yet, and rejecting every database
    on it would block onboarding to catch a typo.
    """
    if target_server_id is None or database_name is None:
        return
    known = db.fetch_one(
        "SELECT count(*) FILTER (WHERE database_name = %s) AS hit, "
        "       count(*) AS total "
        "  FROM (SELECT DISTINCT database_name FROM schema_tables "
        "         WHERE target_server_id = %s) d",
        (database_name, target_server_id))
    if not known or known["total"] == 0:
        return                      # nothing catalogued yet — cannot judge
    if known["hit"] == 0:
        raise ScopeError(
            f"No database named {database_name!r} on this connection. "
            "Leave it empty to cover every database.")


def grant_covers(
    grant: dict,
    required_mode: str,
    target_server_id: int | None = None,
    database_name: str | None = None,
) -> bool:
    """Pure predicate: does this grant row cover a request for
    (required_mode, target_server_id, database_name)? Scope rules:

      - tier: grant.max_tier must be >= required_mode.
      - target: a grant with target_server_id IS NULL covers every target
        (legacy/broad). A target-scoped grant matches ONLY its target — and
        only when the caller passed a matching target_server_id.
      - database: only consulted for a target-scoped grant whose
        database_name is non-NULL; then it must equal the request's db.

    Side-effect-free so it can be unit-tested without a DB."""
    if required_mode not in _TIER_RANK:
        return False
    if not _covers(grant["max_tier"], required_mode):
        return False
    g_target = grant.get("target_server_id")
    if g_target is not None and g_target != target_server_id:
        return False
    g_db = grant.get("database_name")
    if g_target is not None and g_db is not None and g_db != database_name:
        return False
    return True


def active_grants(principal_id: str,
                  at: datetime | None = None) -> list[dict]:
    """Every auto-approve grant live for this principal at `at`.

    The scope filtering happens in Python, so this read is the SAME for every
    (target, database) `effective_grant` is asked about. A caller with many
    questions loads it once and passes it back in — the connections payload
    asked per database and issued 79 identical statements to do it.
    """
    at = at or datetime.now(timezone.utc)
    rows = db.fetch_all(
        """
        SELECT id, slack_user_id, max_tier, target_server_id, database_name,
               starts_at, expires_at, reason, granted_by
          FROM auto_approve_grants
         WHERE slack_user_id = %s
           AND starts_at   <= %s
           AND (expires_at IS NULL OR expires_at > %s)
        """,
        (principal_id, at, at),
    )
    from .teams import use_v2      # lazy: teams -> admins would import this module
    if use_v2():
        rows = rows + _v2_only_waivers(principal_id, at)
    return rows


def _v2_only_waivers(principal_id: str, at: datetime) -> list[dict]:
    """The waivers the legacy table cannot hold, in its row shape.

    Under the new model a waiver can be granted to a TEAM, and a team has no
    `auto_approve_grants` row to put it in: that table is keyed on one person.
    Reading only the legacy table meant a team waiver showed as "auto-approve"
    on every screen that asks the new model, and the submit path, which asked
    here, made every member wait for review anyway.

    Only rows with no legacy source are read. A mirrored row (`mirrored_from`
    set) is a copy of a legacy row already returned above; reading it twice
    would hand a decision two ids for one grant.

    The ids carry an `ag:` prefix so the audit trail and the decision label
    never mistake an `access_grant` id for an `auto_approve_grants` one.
    `team_id` marks a team waiver, which `effective_grant` confirms with the
    resolver before trusting -- see `_team_waiver_applies`.
    """
    return db.fetch_all(
        """
        WITH me AS (
            SELECT i.principal_id AS id FROM principal_identity i
             WHERE i.provider = 'slack' AND i.external_id = %(pid)s
               AND NOT i.is_deleted
        )
        SELECT 'ag:' || g.id AS id, %(pid)s AS slack_user_id,
               g.tier AS max_tier, g.target_id AS target_server_id,
               g.database_name, g.valid_from AS starts_at,
               g.valid_until AS expires_at, g.reason, NULL AS granted_by,
               g.team_id, COALESCE(t.display_name, t.name) AS team_name
          FROM access_grant g
          LEFT JOIN team t ON t.id = g.team_id
         WHERE g.auto_approve AND g.mirrored_from IS NULL
           AND g.revoked_at IS NULL AND NOT g.is_deleted
           AND g.valid_from <= %(at)s
           AND (g.valid_until IS NULL OR g.valid_until > %(at)s)
           AND (g.principal_id IN (SELECT id FROM me)
                OR g.team_id IN (SELECT tm.team_id FROM team_member tm
                                  WHERE tm.principal_id IN (SELECT id FROM me)
                                    AND NOT tm.is_deleted))
        """,
        {"pid": principal_id, "at": at},
    )


def _team_waiver_applies(principal_id: str,
                         target_server_id: int | None,
                         database_name: str | None) -> bool:
    """Whether a team's waiver reaches this member for this database.

    Asked of the access model rather than decided here, because the answer
    depends on a rule `access` owns: a member's OWN grant on a database
    displaces their team's rows there, waivers included. A second copy of that
    rule is a second place for it to be wrong. Only reached when a team waiver
    is the row that would decide, so the batched callers stay batched. Whether
    the waiver's tier covers the request is already settled by `grant_covers`
    on the row itself.
    """
    if target_server_id is None or database_name is None:
        return False
    from . import access
    return access.team_waivers_reach(principal_id, target_server_id, database_name)


def waiver_applies(target, waiver: dict | None) -> bool:
    """May this waiver decide on `target`?

    `decision` asks it of every covering waiver, and so does anything that
    lists or plans waivers (the Slack badge, "already covered" when a new one
    is written), so the two cannot disagree about one.

    - Where the target lets everything skip review
      (`engines.auto_approve_allowed`: the engine's default, or
      `engine_config.auto_approve: true`), every waiver applies, as it always
      has.
    - Where the target says `engine_config.auto_approve: false`, none does.
    - On a target under the archive rule (`engines.archive_rule_applies`) a
      waiver that names it applies, and so does a fleet-wide one, whoever
      holds it (operator rule, 2026-10-01: anyone with a fleet-wide auto RO
      auto-approves RO there). A waiver naming another target never does.
      That only reads can skip review there is `decision`'s to enforce; this
      answers for the waiver, not for the request.

    - On a super-admin-only target (migration 141) a fleet-wide waiver does
      not apply. A waiver that names the target does.

    No target or no waiver means no. A super-admin's own submission is a
    separate rule, decided by the caller.
    """
    if target is None or not waiver:
        return False
    # A fleet-wide waiver stops at a super-admin-only target (migration 141),
    # as a fleet-wide grant does. A waiver that names the target still applies.
    if waiver.get("target_server_id") is None and getattr(target, "id", None) is not None:
        from . import access
        if access.target_is_super_admin_only(target.id):
            return False
    if engines.auto_approve_allowed(target):
        return True
    if not engines.archive_rule_applies(target):
        return False                    # the target itself turned it off
    named = waiver.get("target_server_id")
    return named is None or named == getattr(target, "id", None)


# How the request row and the audit trail name a decision the archive rule's
# role half made: there is no waiver to cite, so the basis is the reason.
ARCHIVE_BASIS = {"owner lead": "archive: owner lead", "admin": "archive: admin"}


def _archive_role_basis(target, holder: str | None, required_mode: str) -> dict | None:
    """The archive rule's role half, as a decision: an admin or the owning
    team's lead auto-approves a read, with no waiver (`access.archive_role`).

    Shaped like a waiver row so every consumer that writes one -- the request
    row, the audit trail, the admin FYI -- writes this too, with `basis` in
    place of a grant id (`audit_details`, `basis_label`, `decided_by_name_for`).
    The role is read as it is now, also for a run scheduled later, as a team
    waiver's reach is.
    """
    if required_mode != "ro" or not holder or not engines.archive_rule_applies(target):
        return None
    target_id = getattr(target, "id", None)
    if target_id is None:
        return None
    from . import access                # lazy, as in _team_waiver_applies
    role = access.archive_role(holder, target_id)
    if role is None:
        return None
    return {"id": None, "basis": ARCHIVE_BASIS[role], "slack_user_id": holder,
            "max_tier": "ro", "target_server_id": target_id, "database_name": None,
            "starts_at": None, "expires_at": None, "reason": ARCHIVE_BASIS[role]}


def decision(
    principal_id: str,
    required_mode: str,
    target,
    database_name: str | None = None,
    *,
    at_time: datetime | None = None,
    rows: list[dict] | None = None,
    refused: list[dict] | None = None,
) -> dict | None:
    """May this request skip review, and on what basis? None means a human
    approves it.

    THE question. `create_request` decides with it, and everything that tells
    somebody beforehand -- `validate_submission`'s justification exemption,
    `/classify`'s `willAutoApprove`, `/connections`' `autoApproveRO`, the Slack
    badge and burst checks, both batch paths -- asks it too, so none of them
    can promise what the submit path would not do. A test reads the package
    and fails a caller of `effective_grant` anywhere but here.

    The answer is the best waiver that may decide on this target
    (`effective_grant` filtered by `waiver_applies`), or, on a target under the
    archive rule with no such waiver, the rule's role basis
    (`_archive_role_basis`). On such a target only a read is ever let through:
    except super-admins, nobody holds more than RO there.

    The fingerprint cache is not part of it: `create_request` consults it after
    this, and only where `engines.auto_approve_allowed` says everything may
    skip review. A super-admin's own submission is the caller's rule too, asked
    first; the role half stands aside for one (`access.archive_role`).

    `at_time` asks about a scheduled run, `rows` supplies the principal's live
    waivers for a caller asking about many scopes (`active_grants`), and
    `refused` collects the covering waivers that may not decide here, so their
    holder can be told why the request waits.
    """
    if target is None:
        return None
    archive = engines.archive_rule_applies(target)
    if archive and required_mode != "ro":
        return None

    def applies(g: dict) -> bool:
        if waiver_applies(target, g):
            return True
        if refused is not None:
            refused.append(g)
        return False

    g = effective_grant(principal_id, required_mode,
                        target_server_id=getattr(target, "id", None),
                        database_name=database_name, at_time=at_time, rows=rows,
                        applies=applies)
    if g is not None or not archive:
        return g
    return _archive_role_basis(target, principal_id, required_mode)


def effective_grant(
    principal_id: str,
    required_mode: str,
    target_server_id: int | None = None,
    database_name: str | None = None,
    at_time: datetime | None = None,
    rows: list[dict] | None = None,
    *,
    applies: Callable[[dict], bool] | None = None,
) -> dict | None:
    """Return the auto-approve grant row that covers (user, mode, target,
    db) at `at_time` (defaults to NOW()). Multiple matches → most permissive
    (highest max_tier, then latest expires_at). A target-scoped grant only
    matches when `target_server_id` is supplied and equal; broad (NULL-scope)
    grants match any target.

    `rows` supplies the principal's live grants instead of reading them, for a
    caller asking about many scopes at once — see `active_grants`.

    `applies` is asked of each covering row in that order, and a row it refuses
    is passed over, so the answer is the best waiver that may decide HERE.
    Filtering the answer instead would lose a waiver: a fleet-wide row that
    does not apply sorts ahead of a narrower one that does.

    Called by `decision` and nothing else, which passes `waiver_applies` for
    the target and adds the archive rule's role half. A caller asking this
    directly would decide without either."""
    if required_mode not in _TIER_RANK:
        return None
    at = at_time or datetime.now(timezone.utc)
    # We can't easily encode the tier-rank check in SQL portably, so we
    # filter in Python — the table is small (typically a handful of
    # active grants).
    rows = active_grants(principal_id, at) if rows is None else rows
    candidates = [
        r for r in rows
        if grant_covers(r, required_mode, target_server_id, database_name)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda r: (
            -_TIER_RANK[r["max_tier"]],
            # NULL expires_at means infinity — sort it last (most permissive).
            r["expires_at"] or datetime.max.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    # A team waiver is trusted only once the resolver agrees it reaches this
    # member here. The answer is the same for every team row in the list, so
    # it is asked at most once. It is asked about NOW: for a run scheduled
    # later, a team waiver that has not started yet fails closed and the run
    # waits for review.
    team_ok = None
    for c in candidates:
        if c.get("team_id") is not None:
            if team_ok is None:
                team_ok = _team_waiver_applies(principal_id,
                                               target_server_id, database_name)
            if not team_ok:
                continue
        # Asked after the reach test, so a caller that keeps the refused rows
        # (to tell their holder why) only ever sees waivers they really hold.
        if applies is not None and not applies(c):
            continue
        return c
    return None


def fmt_until(expires_at: datetime | None) -> str:
    """Short human label for the modal banner / DM text."""
    if expires_at is None:
        return "no expiry"
    return f"until `{expires_at:%Y-%m-%d %H:%M UTC}`"


# Sentinel used as the requests.decided_by_slack_id when the auto-approve
# path fills in approval columns. Stored verbatim; the UI / audit_log
# tooling treats it as a non-Slack-user marker.
AUTO_DECIDED_BY = "AUTO"


def decided_by_name_for(grant: dict) -> str:
    """Human-readable label written to requests.decided_by_name (and
    decision_reason) when `decision` short-circuits the admin gate: the waiver
    it cites, or the archive rule's basis when no waiver decided."""
    if grant.get("basis"):
        return f"auto-approved ({grant['basis']}, max_tier={grant['max_tier']})"
    until = fmt_until(grant.get("expires_at"))
    if grant.get("team_id") is not None:
        return (f"auto-approved (team {grant.get('team_name') or grant['team_id']} "
                f"waiver {grant['id']}, max_tier={grant['max_tier']}, {until})")
    return f"auto-approved (grant #{grant['id']}, max_tier={grant['max_tier']}, {until})"


def audit_details(grant: dict) -> dict:
    """What an `auto_approved` audit row (and its `submitted` row) records
    about the decision: the waiver's `grant_id`, or the archive rule's `basis`,
    which has no waiver to cite. Never both, so a reader of the trail can tell
    a waiver's decision from a role's."""
    if grant.get("basis"):
        return {"basis": grant["basis"], "max_tier": grant["max_tier"]}
    return {"grant_id": grant["id"], "max_tier": grant["max_tier"]}


def basis_label(grant: dict) -> str:
    """The short form, for a DM: `grant #12`, or `archive: admin`."""
    return grant.get("basis") or f"grant #{grant['id']}"


# ---------------------------------------------------------------------------
# Fingerprint approval cache (Smart Routing 3b)
# ---------------------------------------------------------------------------


def fingerprint_cache_enabled() -> bool:
    val = (cfg.get_setting("fingerprint_cache_enabled", "on") or "").strip().lower()
    return val in {"on", "true", "yes", "1"}


def fingerprint_cache_hit(
    principal_id: str,
    target_server_id: int,
    database_name: str,
    fingerprint: str,
) -> dict | None:
    """Return the most recent prior request that lets this RO query skip
    admin approval, or None. A hit requires: same requester + target +
    database + query_fingerprint, a `completed` status, and completion
    within fingerprint_cache_ttl_days. CALLER must have already
    confirmed required_mode == 'ro' — this never gates writes.

    Returns a dict with the matched request id + completed_at so the
    caller can cite it in the audit log + admin FYI DM."""
    if not fingerprint or not fingerprint_cache_enabled():
        return None
    ttl_days = cfg.get_int("fingerprint_cache_ttl_days", 30)
    return db.fetch_one(
        "SELECT id, completed_at FROM requests "
        " WHERE requester_slack_id = %s "
        "   AND target_server_id   = %s "
        "   AND database_name      = %s "
        "   AND query_fingerprint  = %s "
        "   AND status = 'completed' "
        "   AND completed_at >= NOW() - make_interval(days => %s) "
        " ORDER BY completed_at DESC LIMIT 1",
        (principal_id, target_server_id, database_name, fingerprint, ttl_days),
    )


def decided_by_name_for_fingerprint(prior_request_id: int) -> str:
    """Label for requests.decided_by_name when the fingerprint cache
    short-circuits the admin gate."""
    return f"auto-approved (fingerprint match of completed request #{prior_request_id})"
