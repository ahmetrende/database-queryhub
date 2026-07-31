"""POST /endpoint-requests — the "Request new endpoint" modal.

Reuses the Slack blocked-query access-request flow end to end: same
access_requests storage, same admin DMs with approve/reject buttons,
same dedupe. The requested server may be free text (the whole point is
asking for something you can't see); when it matches a known alias the
admins get the resolved target card.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import access_requests
from . import deps
from .routes_data import _target_by_alias
from .routes_queries import _bot_client

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(deps.block_pw_gate)])


class EndpointRequestIn(BaseModel):
    server: str = Field(min_length=1, max_length=200)
    database: str | None = Field(default=None, max_length=200)
    tier: str = Field(pattern="^(RO|RW|DDL)$")
    reason: str = Field(min_length=5, max_length=4000)


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
