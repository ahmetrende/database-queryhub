"""The highest tier an approver may approve.

An approver who is not a super-admin approves RO or RW. Never DDL: a schema
change always needs a super-admin (operator decision, 2026-10-24). Two paths
in the app write such a ceiling, and both call this module:

  - POST and PUT /admin/scopes, a scoped admin ("DBA scope"): `check_scope_tier`
  - POST and PATCH /admin/roles, a role `approver`: `check_approver_ceiling`

A refusal is HTTP 400 with the code `tier_not_allowed`, the same code that an
auto-approve rule for DDL gets (auto_approve_limits.py).

A role `admin` is not an approver in this sense. An admin with no ceiling is a
super-admin, and a super-admin approves DDL. This module does not touch it.

The rule lives in the application only. The database has no CHECK on
`admins.max_tier` or `role_assignment.max_tier` that refuses `ddl`, and no
trigger. A person who writes a row with SQL gets an approver with a DDL ceiling,
and every reader honours it. That back door is deliberate, the same as for an
auto-approve rule (docs/OPERATIONS.md, section 30). A temporary admin grant is
written by SQL (admins.grant_temp_admin has no caller in the app), so it stays
outside the rule. Rows that exist now keep working.
"""
from __future__ import annotations

# An approver's ceiling is RO or RW.
APPROVER_TIERS = ("ro", "rw")

SCOPE_MESSAGE = "An admin scope can approve RO or RW. Schema changes need a super-admin."
ROLE_MESSAGE = ("An approver needs a tier ceiling of RO or RW. "
                "Schema changes need a super-admin.")
CODE = "tier_not_allowed"


class TierRefused(ValueError):
    """The tier is not allowed. `code` and `message` go to the client."""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = CODE
        self.message = message


def check_scope_tier(max_tier: str | None) -> None:
    """Refuse a DBA scope whose highest tier is DDL (or missing).

    `max_tier` is the lower-case tier that the route derives from `canApprove`.
    """
    if (max_tier or "").lower() not in APPROVER_TIERS:
        raise TierRefused(SCOPE_MESSAGE)


def check_approver_ceiling(tier: str | None) -> None:
    """Refuse a role `approver` with no ceiling or a DDL ceiling.

    `tier` is the lower-case ceiling, or None for "no ceiling". No ceiling means
    every tier, so it is DDL too.
    """
    if (tier or "").lower() not in APPROVER_TIERS:
        raise TierRefused(ROLE_MESSAGE)
