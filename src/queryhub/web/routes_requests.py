"""Asking for access: `GET /requestable` and `POST /endpoint-requests`, and the
requester's own side of it: `GET /endpoint-requests` lists their requests,
`POST /endpoint-requests/{id}/withdraw` withdraws a pending one.

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

from .. import access_requests, grants, people, targets, teams
from . import deps, mapping
from .routes_data import _alias_of, _catalog_databases, _target_by_alias
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


_REQUESTABLE_TAG_KEYS = ("provider", "service")


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
    row = {"connectionId": t.alias, "name": t.alias, "engine": t.engine,
           "env": mapping.env_of(t.alias), "databases": dbs,
           "partial": granted is not None}
    # Where it runs, so the picker can draw the provider logo. Provider and
    # service only, the same narrowing GET /connections applies to a non-admin:
    # `account` and any custom key are registry detail a requester does not see.
    tags = {k: v for k, v in (getattr(t, "tags", None) or {}).items()
            if k in _REQUESTABLE_TAG_KEYS and v}
    if tags:
        row["tags"] = tags
    return row


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
                              f"Refused: you already have {open_n} pending access "
                              f"request(s). The limit is {max_open}. Wait for the DBA "
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
                              "Refused: you cannot request access to that connection.")
        wanted = (body.database or "").strip()
        known = _catalog_databases(t.id)
        if wanted and known and wanted not in known:
            # Same rule the auto-approve form got, INCLUDING its exemption: a
            # database that is not on the connection is a typo worth catching
            # here — but a connection with no catalogue yet has no list to
            # check against, and refusing every name on it would make a
            # freshly onboarded server the one thing nobody can request. The
            # snapshot arrives on the next hourly sync; until then the field is
            # free text, exactly as it is for a server QueryHub does not have.
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
    #
    # `notify_admins` imports slack_app only when it has a Slack client to send
    # with. So this route works when Slack is off, and also without slack_app.
    from .. import admins as admins_mod
    ts = access_requests.notify_admins(_bot_client(), row, t, requested_server=server)
    if not admins_mod.list_active():
        raise deps._error(503, "server_error",
                          "QueryHub saved your request, but no active admin "
                          "can review it. Contact the DBA team.")
    return {"id": f"er_{row['id']}", "status": "submitted",
            "slackMessageTs": ts}


# ---- The requester's own access requests -------------------------------------

def _access_request_id(raw: str) -> int | None:
    """The id as the lists give it (`er_17`), or the bare number (`17`).
    None for anything else. Only the digits 0 to 9 count: `str.isdigit()`
    also accepts a superscript two, and `int()` then raises."""
    value = (raw or "").strip()
    if value.startswith("er_"):
        value = value[len("er_"):]
    return int(value) if value.isascii() and value.isdigit() else None


def _own_entries(rows: list[dict]) -> list[dict]:
    """The rows in the requester's shape, with one name lookup for the page."""
    name_of = people.namer(
        [r.get("requester_name") or r.get("requester_slack_id") for r in rows]
        + [r.get("decided_by_name") or r.get("decided_by_slack_id") for r in rows])
    return [mapping.own_endpoint_request_entry(r, _alias_of, name_of) for r in rows]


@router.get("/endpoint-requests")
def my_endpoint_requests(claims: dict = Depends(deps.current_user)):
    """The caller's own access requests, newest first: the status, the
    decision note, who decided, and when.

    Only the caller's rows. The admin list is `GET /admin/endpoint-requests`."""
    deps.require_whitelisted(claims)
    return {"requests": _own_entries(access_requests.list_for(claims["sub"]))}


def _closed_message(status: str | None) -> str:
    if status == "withdrawn":
        return "You already withdrew this request."
    return "This request already has a decision."


def _retire_access_request_cards(row: dict, claims: dict) -> None:
    """Take a withdrawn access request off the admins' Slack cards.

    The same step as `routes_queries._retire_admin_cards` for a withdrawn SQL
    request, on the cards of an access request: the Approve and Reject buttons
    go, and a line says that nobody has to act. Without it an admin presses
    Approve on a request that is gone and reads "already decided".

    Failures are logged and swallowed. The withdrawal is committed, and a
    Slack problem must not turn it into an error for the requester."""
    client = _bot_client()
    if client is None:          # vanilla profile: no Slack, no cards to retire
        return
    from ..slack_app import access
    who = claims.get("name") or claims.get("sub") or "The requester"
    try:
        target = (targets.get(row["target_server_id"])
                  if row.get("target_server_id") else None)
        access.update_admin_cards(
            client, row["id"], target,
            f":wastebasket: {who} withdrew this request. You do not need to act.")
    except Exception:
        log.exception("could not retire the admin cards of withdrawn access "
                      "request %s", row.get("id"))


@router.post("/endpoint-requests/{req_id}/withdraw")
def withdraw_endpoint_request(req_id: str,
                              claims: dict = Depends(deps.current_user)):
    """Withdraw one of your own pending access requests.

    404 for an id that is unknown or that belongs to another person, so this
    route tells nobody which requests exist. 409 when the request is not
    pending any more: an admin decided it, or you withdrew it already.

    The status change and its audit row commit in one transaction
    (`access_requests.withdraw`). Then the admins' Slack cards lose their
    buttons. The answer is the row in the shape of `GET /endpoint-requests`."""
    deps.require_whitelisted(claims)
    uid = claims["sub"]
    rid = _access_request_id(req_id)
    row = access_requests.get(rid) if rid is not None else None
    if rid is None or row is None or row["requester_slack_id"] != uid:
        raise deps._error(404, "not_found", "No such access request.")
    if row["status"] != "pending":
        raise deps._error(409, "conflict", _closed_message(row["status"]))
    done = access_requests.withdraw(rid, uid, claims.get("name"))
    if done is None:
        # Lost the race: an admin decided it between the read and the UPDATE.
        fresh = access_requests.get(rid) or row
        raise deps._error(409, "conflict", _closed_message(fresh["status"]))
    _retire_access_request_cards(done, claims)
    return _own_entries([done])[0]


# ---- Auto-approve window requests (requester) --------------------------------

class WindowRequestIn(BaseModel):
    connectionId: str
    databaseId: str | None = None
    tier: str = "RO"
    # Either one of the web's whole-day windows or one of Slack's short ones.
    days: int | None = None
    windowMinutes: int | None = None
    reason: str


def _window_options() -> list[dict]:
    from .. import auto_approve_requests, window_options
    return ([{"minutes": m, "label": lbl} for m, lbl in window_options.WINDOW_OPTIONS]
            + [{"minutes": d * 1440, "label": window_options.window_label(d * 1440), "days": d}
               for d in auto_approve_requests.DAY_WINDOWS])


def _days(minutes) -> int | None:
    return minutes // 1440 if minutes and minutes % 1440 == 0 else None


@router.get("/auto-approve-requests")
def my_window_requests(claims: dict = Depends(deps.current_user)):
    """The requester's own window requests, the lengths on offer, and the
    tiers on offer.

    Asking for a window was Slack-only; this is the web half. The lengths come
    from the same list the Slack modal offers, so the two surfaces cannot
    drift into offering different windows. `tiers` is `["RO"]`, or
    `["RO", "RW"]` while `auto_approve_rw_requests` is on. A RW window also
    needs RW access on the database, which the connection list shows.
    """
    uid = claims["sub"]
    from .. import auto_approve_requests
    rows = auto_approve_requests.list_for(uid)
    return {
        "windowOptions": _window_options(),
        "tiers": auto_approve_requests.offered_tiers(),
        "requests": [{
            "id": r["id"], "connectionId": r["alias"], "databaseId": r["database_name"],
            "tier": (r["max_tier"] or "ro").upper(), "windowMinutes": r["window_minutes"],
            "days": _days(r["window_minutes"]),
            "reason": r["reason"], "status": r["status"],
            "decidedByName": r["decided_by_name"],
            "decidedAt": mapping.iso(r["decided_at"]),
            "createdAt": mapping.iso(r["created_at"]),
        } for r in rows],
    }


@router.post("/auto-approve-requests", status_code=201)
def create_window_request(body: WindowRequestIn,
                          claims: dict = Depends(deps.current_user)):
    """Ask for an auto-approve window from the web.

    Granting stays a DBA's decision: this files the request and sends every
    active admin the same approve / reject card the Slack modal sends, and the
    approval happens there. The rules are the Slack modal's own
    (`auto_approve_requests.submit_window`), so a request the one surface
    refuses the other refuses too.

    `tier` is RO by default. RW is accepted only while
    `auto_approve_rw_requests` is on, and only from a person who holds RW
    there. Otherwise the answer is 403 with `field: "tier"`. DDL is always
    refused. On a production connection, only a super-admin decides a RW
    window.
    """
    uid = claims["sub"]
    from .. import auto_approve_requests, targets
    t = targets.by_alias(body.connectionId)
    if t is None:
        raise deps._error(404, "not_found", "Unknown connection.")
    minutes = (body.days * 1440 if body.days is not None
               else body.windowMinutes if body.windowMinutes is not None else 60)
    try:
        row, t = auto_approve_requests.submit_window(
            principal_id=uid, name=claims.get("name"), target_id=t.id,
            window_minutes=minutes, reason=body.reason,
            database_name=body.databaseId, tier=body.tier)
    except auto_approve_requests.WindowRequestRefused as e:
        raise deps._error(e.status, "window_refused", e.message, field=e.field)
    from .routes_queries import _bot_client
    # Called also with no Slack client: the IdP panel's outbox row does not
    # need Slack. With no client, notify_admins DMs nobody and returns 0.
    reached = auto_approve_requests.notify_admins(_bot_client(), row, t.alias)
    return {"id": row["id"], "status": row["status"], "connectionId": t.alias,
            "databaseId": row["database_name"], "tier": (row["max_tier"] or "ro").upper(),
            "windowMinutes": row["window_minutes"], "days": _days(row["window_minutes"]),
            "adminsNotified": reached}
