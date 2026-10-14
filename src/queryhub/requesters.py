"""Allowlist of Slack users who can invoke /sql. Replaces the
REQUESTER_ALLOWLIST env var.

Rules (fail CLOSED):
  - Admins always pass (managed in the `admins` table separately) — this also
    bootstraps a fresh install: seed the first admin, no open window needed.
  - Otherwise ONLY users with an enabled `requesters` row are allowed.
  - An empty requesters table grants NO ONE access (it used to open the bot to
    every workspace user; that was a fail-open footgun if the table ever
    emptied). Access to the SQL gateway never widens on its own.

Emails are populated lazily by `profile_sync.maybe_backfill_email`.
"""
from __future__ import annotations

from typing import NamedTuple

from . import admins, db


def is_allowed(principal_id: str) -> bool:
    # Admins always pass — this is also how a fresh install bootstraps (seed
    # the first admin directly), so the allowlist can safely fail CLOSED.
    if admins.is_admin(principal_id):
        return True
    # Fail CLOSED: only an explicitly-enabled requester gets in. An empty
    # requesters table does NOT grant everyone access. (It used to — "empty
    # allowlist == open mode" — which is a fail-open footgun: if the table is
    # ever emptied by accident, a bulk disable, or a bad cleanup, the whole
    # web app AND Slack /sql would open to every workspace user. Access to a
    # production SQL gateway must never widen on its own.)
    found = db.fetch_one(
        "SELECT 1 FROM requesters "
        "WHERE slack_user_id = %s AND enabled = TRUE LIMIT 1",
        (principal_id,),
    )
    return found is not None


def bypasses_team_grants(principal_id: str) -> bool:
    """True if the user's `requesters` row has `bypass_team_grants=TRUE`.
    Such users see every enabled target regardless of team_target_grants
    (mirrors admin visibility) but stay non-admin (cannot approve/reject)."""
    if not principal_id:
        return False
    row = db.fetch_one(
        "SELECT 1 FROM requesters "
        "WHERE slack_user_id = %s AND enabled = TRUE AND bypass_team_grants = TRUE "
        "LIMIT 1",
        (principal_id,),
    )
    return row is not None


def open_request_count(principal_id: str) -> int:
    """Number of in-flight requests for this user — pending admin review,
    awaiting scheduled execution, or currently executing. Used by the
    per-user rate limit so a single requester (or compromised account)
    can't fill the queue. Completed/failed/rejected/cancelled don't
    count."""
    row = db.fetch_one(
        "SELECT count(*) AS n FROM requests "
        "WHERE requester_slack_id = %s "
        "  AND status IN ('pending', 'changes_requested', 'approved', "
        "                 'scheduled', 'executing')",
        (principal_id,),
    )
    return row["n"] if row else 0


def get(principal_id: str) -> dict | None:
    return db.fetch_one(
        "SELECT slack_user_id, email, name, enabled, added_at, added_by "
        "FROM requesters WHERE slack_user_id = %s",
        (principal_id,),
    )


def by_email(email: str) -> dict | None:
    """Find the principal that owns an address — the join an external SSO
    login lands on.

    An OIDC provider proves an email; every grant, approval, audit row and
    masking exemption hangs off the principal id. This is the only bridge
    between the two, which makes it authorization-critical rather than a
    convenience lookup, so it is deliberately strict:

      - case- and whitespace-insensitive, because an IdP is free to send
        `Name.Surname@example.com` and the column stores it lowered;
      - AMBIGUITY IS A REFUSAL. Two rows sharing an address cannot be
        resolved into one identity, and picking either would hand one
        person another person's grants. None of them is the safe answer;
      - it does NOT create anything. An address with no row is not a new
        user, it is a stranger — see the caller, which turns that into a
        refusal rather than an onboarding.

    Enabled and disabled rows both match on purpose: a disabled requester
    must fail the entry gate as themselves, not read as an unknown address.
    """
    e = (email or "").strip().lower()
    if not e:
        return None
    rows = db.fetch_all(
        "SELECT slack_user_id, email, name, enabled "
        "FROM requesters WHERE lower(btrim(email)) = %s",
        (e,),
    )
    if len(rows) != 1:
        return None
    return rows[0]


class EmailMatch(NamedTuple):
    """What one address resolves to across BOTH people tables.

    `row` is the one principal's row, or None. `ambiguous` tells the two kinds
    of None apart: True when rows of two or more DIFFERENT principals share
    the address, False when no row has it. Each needs a different fix: an
    admin adds an unknown person, and fixes the rows of a shared address.
    """

    row: dict | None
    ambiguous: bool


def _resolve(req: list[dict], adm: list[dict]) -> EmailMatch:
    """The rule, applied to the rows that share one address: answer only when
    every row is the same principal. The requester row wins when there is one,
    since it carries `enabled` for the entry gate."""
    principals = {r["slack_user_id"] for r in req + adm}
    if len(principals) > 1:
        return EmailMatch(None, True)
    if not principals:
        return EmailMatch(None, False)
    return EmailMatch((req or adm)[0], False)


def match_email(email: str) -> EmailMatch:
    """`principal_by_email`, with the reason when there is no answer.

    The IdP assertion uses it, so that the person learns which fix they need.
    `principal_by_email` returns this function's `row`, so the two cannot
    disagree about who an address belongs to.
    """
    e = (email or "").strip().lower()
    if not e:
        return EmailMatch(None, False)
    req = db.fetch_all(
        "SELECT slack_user_id, email, name, enabled "
        "FROM requesters WHERE lower(btrim(email)) = %s", (e,))
    adm = db.fetch_all(
        "SELECT slack_user_id, email, name, enabled "
        "FROM admins WHERE lower(btrim(email)) = %s", (e,))
    return _resolve(req, adm)


def principal_by_email(email: str) -> dict | None:
    """The one principal an address belongs to, across BOTH people tables.

    `by_email` here and `admins.by_email` each refuse two rows sharing an
    address inside their own table. Chained with `or`, they did not refuse one
    row in EACH table naming different people: the first table's answer won.
    Two callers also asked in opposite orders, so the same data resolved to
    different principals. This reads both tables and answers only when every
    match is the same principal. The requester row is returned when there is
    one, since it carries `enabled` for the entry gate. Every external sign-in
    (OIDC) and the principal sync resolve through here.

    None means "no row" and "two people" alike. `match_email` holds the rule
    and tells the two apart. The IdP assertion calls it directly.
    """
    return match_email(email).row


def duplicate_addresses() -> list[dict]:
    """Every address for which `match_email` answers `ambiguous`, with the
    rows that share it.

    The IdP assertion refuses such an address with `ambiguous_address`, and an
    external sign-in refuses it as `not_onboarded`. Neither tells an admin
    which rows to fix, and `GET /admin/people` carries no addresses.

    The key is the one the lookup compares, `lower(btrim(email))`, and the
    rule is the same `_resolve`. So one principal with a row in each table is
    not a duplicate, and a row with no address is never one. Each entry is
    `{"email", "rows"}`, and each row carries `source` (the table),
    `slack_user_id`, `name` and `enabled`.
    """
    rows = db.fetch_all(
        "SELECT lower(btrim(email)) AS address, 'requesters' AS source, "
        "       slack_user_id, name, enabled "
        "  FROM requesters WHERE lower(btrim(email)) <> '' "
        "UNION ALL "
        "SELECT lower(btrim(email)), 'admins', slack_user_id, name, enabled "
        "  FROM admins WHERE lower(btrim(email)) <> ''")
    by_address: dict[str, list[dict]] = {}
    for r in rows:
        if r["address"]:        # the SQL leaves out rows with no address, too
            by_address.setdefault(r["address"], []).append(r)
    out: list[dict] = []
    for address in sorted(by_address):
        # The lookup normalises its input with Python's strip().lower(). A key
        # that changes under that normalisation is one that no lookup reaches,
        # so it never causes the refusal.
        if address != address.strip().lower():
            continue
        group = sorted(by_address[address], key=lambda r: r["slack_user_id"])
        req = [r for r in group if r["source"] == "requesters"]
        adm = [r for r in group if r["source"] == "admins"]
        if _resolve(req, adm).ambiguous:
            out.append({"email": address, "rows": req + adm})
    return out


def list_enabled_ids() -> set[str]:
    """Every principal with an enabled row — what the Layer-A reconcile
    compares the panel's desired state against."""
    rows = db.fetch_all("SELECT slack_user_id FROM requesters WHERE enabled = TRUE")
    return {r["slack_user_id"] for r in rows}


_ENABLE_SQL = "UPDATE requesters SET enabled = TRUE WHERE slack_user_id = %s"
_DISABLE_SQL = "UPDATE requesters SET enabled = FALSE WHERE slack_user_id = %s"


def enable(principal_id: str) -> None:
    """Re-enable an existing row.

    Deliberately an UPDATE and not an upsert: creating a requester needs a
    Slack id (approvals and results are DMs), and the reconcile's caller does
    not have one to offer. An address with no row is reported back for a human
    to onboard rather than turned into a half-formed row here.
    """
    db.execute(_ENABLE_SQL, (principal_id,))


def enable_in(cur, principal_id: str) -> None:
    """`enable` on the caller's cursor. The change commits in the caller's
    transaction, together with its audit row."""
    cur.execute(_ENABLE_SQL, (principal_id,))


def disable(principal_id: str) -> None:
    """Revoke the entry gate for a principal. The row stays, so the person
    still reads as themselves rather than as an unknown address."""
    db.execute(_DISABLE_SQL, (principal_id,))


def disable_in(cur, principal_id: str) -> None:
    """`disable` on the caller's cursor. The change commits in the caller's
    transaction, together with its audit row."""
    cur.execute(_DISABLE_SQL, (principal_id,))
