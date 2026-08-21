"""Asking for access: `GET /requestable` and `POST /endpoint-requests`.

Reuses the Slack blocked-query access-request flow end to end: same
access_requests storage, same admin DMs with approve/reject buttons, same
dedupe, and — because `requested_tier` is persisted — the same one-click
approval that writes the real grant.

Two shapes, and the difference is the whole point:

* **Pick it from the catalogue.** `GET /requestable` lists what this person
  could ask for: enabled targets, minus the ones they already reach, minus the
  control plane, minus the maintenance databases. `connectionId` on the POST
  then names a real target and the request arrives machine-resolvable, so
  approval is a click rather than an admin re-typing an alias.
* **Free text**, unchanged, for a database QueryHub does not have yet — the
  case the modal was originally built for, and still the only way to ask for
  something you cannot see.

The catalogue is why this is not purely a UI change: until it existed, a
requester had to type an alias they had never been shown, and the resulting
`svc-prod-notifcation` was resolved by hand at the other end.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import access_requests, grants, targets, teams
from . import deps, mapping
from .routes_data import _catalog_databases, _target_by_alias
from .routes_queries import _bot_client

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(deps.block_pw_gate)])


class EndpointRequestIn(BaseModel):
    server: str = Field(min_length=1, max_length=200)
    database: str | None = Field(default=None, max_length=200)
    tier: str = Field(pattern="^(RO|RW|DDL)$")
    reason: str = Field(min_length=5, max_length=4000)
    # Set when the row came from GET /requestable. Authoritative: it names a
    # target by alias or id, so the request cannot be a near-miss of one. The
    # free-text `server` stays required and carries the same value, which keeps
    # every existing client working and keeps one field to display.
    connectionId: str | None = Field(default=None, max_length=200)


def _target_by_id(value: str):
    """Resolve a numeric connection id, or None. The catalogue sends aliases,
    but a client holding an id from /connections should not have to translate."""
    if not value.isdigit():
        return None
    try:
        return targets.get(int(value))
    except Exception:
        return None


def _requestable_target(t, granted, control_plane) -> dict | None:
    """One catalogue row, or None when there is nothing left to ask for.

    `granted` is the user's resolved access for this target: None when they
    hold nothing, a set of database names when the grant is scoped, or the
    empty set for an unrestricted grant (everything, nothing left to ask).
    """
    if t.id in control_plane:
        # The bot's own metadata database. A grant here would let someone edit
        # the audit trail that records their own queries, so it is not offered
        # even as a request — admins.grant refuses it anyway, and a form that
        # offers an option the backend always rejects is worse than one that
        # does not list it.
        return None
    dbs = _catalog_databases(t.id)
    if granted is not None:
        if not granted:
            return None                      # unrestricted: nothing to ask for
        dbs = [d for d in dbs if d not in granted]
        if not dbs:
            return None                      # every catalogued db already held
    # `env` is derived from the alias, the same way GET /connections does it —
    # it is not a column on the target row.
    return {"connectionId": t.alias, "name": t.alias, "engine": t.engine,
            "env": mapping.env_of(t.alias), "databases": dbs,
            "partial": granted is not None}


@router.get("/requestable")
def requestable(claims: dict = Depends(deps.current_user)):
    """What this person could ask for access to.

    Everything enabled that they cannot already reach. Excluded: disabled
    targets (asking for a retired server is never the intent), the control
    plane, the maintenance databases QueryHub hides everywhere, and any
    target/database pair they already hold — the list is meant to answer "what
    is missing", so showing what they have is noise that hides the answer.

    `partial: true` marks a target they already hold with a narrower database
    scope, so the UI can say "you have some of this one" instead of implying
    they have none of it.
    """
    deps.require_whitelisted(claims)
    uid = claims["sub"]
    control_plane = grants.control_plane_target_ids()
    out = []
    for t in targets.list_enabled():
        grant = teams.effective_grant_for_user(uid, t.id)
        granted = None
        if grant is not None:
            allowed = grant["allowed_databases"]
            granted = set(allowed) if allowed is not None else set()
        row = _requestable_target(t, granted, control_plane)
        if row is not None:
            out.append(row)
    return {"connections": out}


@router.post("/endpoint-requests", status_code=201)
def endpoint_request(body: EndpointRequestIn,
                     claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    uid = claims["sub"]
    server = body.server.strip()

    # Abuse guard: each endpoint request fans out a DM to EVERY admin, so
    # cap how many a non-admin can have open at once (mirrors the /sql
    # in-flight cap). Without this a whitelisted client could script a POST
    # per target alias and spam every admin.
    from .. import admins
    from .. import config as cfg
    if not admins.is_admin(uid):
        max_open = cfg.get_int("max_open_access_requests_per_user", 5)
        open_n = access_requests.open_count_for(uid)
        if open_n >= max_open:
            raise deps._error(409, "conflict",
                              f"You already have {open_n} pending access "
                              f"request(s) (max {max_open}). Wait for the DBA "
                              "team to review them.")

    # A picked row is authoritative: resolve it, and refuse rather than fall
    # back to free text. Falling back would turn "the connection you chose no
    # longer exists" into a request for a server nobody can match, reviewed by
    # hand — the exact outcome the catalogue exists to remove.
    picked = (body.connectionId or "").strip()
    if picked:
        t = _target_by_alias(picked) or _target_by_id(picked)
        if t is None or not t.enabled:
            raise deps._error(404, "not_found",
                              f"Unknown connection '{picked}'.")
        if t.id in grants.control_plane_target_ids():
            raise deps._error(400, "bad_request",
                              "That connection cannot be requested.")
        wanted = (body.database or "").strip()
        if wanted and wanted not in _catalog_databases(t.id):
            # Same rule the auto-approve form got: a database that is not on
            # the connection is a typo worth catching here, not a grant that
            # silently matches nothing later.
            raise deps._error(400, "unknown_database",
                              f"'{wanted}' is not a database on {t.alias}.")
        server = t.alias
    else:
        t = _target_by_alias(server)
    reason = f"[requested tier: {body.tier}] {body.reason.strip()}"
    if t is None:
        reason = f"[server: {server}] {reason}"

    row = access_requests.create(
        principal_id=uid,
        name=claims.get("name"),
        target_server_id=t.id if t else None,
        database_name=(body.database or "").strip() or None,
        # The dedupe unique index keys on (user, md5(attempted_query),
        # target_server_id, database_name). For free-text servers target
        # is NULL and query is empty, so two DIFFERENT unknown servers
        # would collide into one 409. Put a canonical discriminator here
        # so distinct (server, tier, db) requests are distinct.
        attempted_query=(None if t else
                         f"web-endpoint-request:{server}|{body.tier}|"
                         f"{(body.database or '').strip()}"),
        reason=reason,
        # Persisted so approval can auto-grant exactly what was asked
        # (the reason prefix above stays for display).
        requested_tier=body.tier.lower(),
    )
    if row is None:
        raise deps._error(409, "conflict",
                          "You already have an identical pending access request.")

    # "Saved" and "reviewable" decide the status code. Whether a Slack DM went
    # out is a third, separate question.
    #
    # This used to 503 whenever the fan-out returned no message ts — which in
    # the vanilla profile is always, since there is no Slack client to send
    # with. The row was created, GET /api/admin/endpoint-requests showed it to
    # the DBA and web approval worked, but the requester was told "no admins are
    # available to review it right now" on the default zero-dependency profile
    # the README recommends. 503 is only honest when nobody can act on it.
    from .. import admins as admins_mod
    from ..slack_app import access
    ts = access.fan_out_admin_dms(_bot_client(), row, t, requested_server=server)
    if not admins_mod.list_active():
        raise deps._error(503, "server_error",
                          "Your request was saved but there are no active "
                          "admins to review it. Contact the DBA team.")
    return {"id": f"er_{row['id']}", "status": "submitted",
            "slackMessageTs": ts}
