"""A person's web workspace and UI preferences, kept on the server.

The editor keeps its open tabs ("workspace") and its UI preferences ("prefs")
in browser storage. These routes keep one copy of each per person in
`user_workspaces` (migration 221), so the state follows the person to another
browser.

- GET    /api/me/workspace/{kind}  the stored copy, or version 0 when none.
- PUT    /api/me/workspace/{kind}  store a new copy. The body names the
  version that the client read. A different stored version answers 409 with
  the stored copy, so two browsers cannot silently overwrite each other.
- DELETE /api/me/workspace/{kind}  forget the copy on this server.

A person reads and writes only the row of their own principal: the same
`claims["sub"]` that /api/me returns as `slackId`. No route takes a principal
from the request.

No audit row: this is the person's own editor state, like browser storage.
It changes no access, no data on a target and nobody else's view.

bot_config (runtime-effective):
- `workspace_sync_enabled` (on): off = GET answers `{enabled: false}` and
  PUT answers 404 `disabled`. DELETE still works, so that a person can
  always remove what the server kept.
- `workspace_max_kb` (512): the largest `data`, in KB of JSON text.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import config as cfg
from .. import db
from . import deps

router = APIRouter(prefix="/api/me/workspace",
                   dependencies=[Depends(deps.block_pw_gate)])

KINDS = frozenset({"workspace", "prefs"})

_DEFAULT_MAX_KB = 512


class WorkspaceIn(BaseModel):
    # `data` is checked in the route, not here, so that a list or a string
    # answers 400 with a plain message instead of a pydantic 422.
    data: Any = None
    baseVersion: int = Field(ge=0)


# ---- settings ----------------------------------------------------------------

def _enabled() -> bool:
    return cfg.get_bool("workspace_sync_enabled", True)


def _max_bytes() -> int:
    try:
        kb = cfg.get_int("workspace_max_kb", _DEFAULT_MAX_KB)
    except ValueError:
        kb = _DEFAULT_MAX_KB
    return max(1, kb) * 1024


# ---- storage -----------------------------------------------------------------
# Small functions so that the tests can replace the database with a dict.

def _read(principal_id: str, kind: str) -> dict[str, Any] | None:
    return db.fetch_one(
        "SELECT data, version, updated_at FROM user_workspaces"
        " WHERE principal_id = %s AND kind = %s",
        (principal_id, kind))


_UPSERT_SQL = (
    "INSERT INTO user_workspaces (principal_id, kind, data)"
    " VALUES (%s, %s, %s::jsonb)"
    " ON CONFLICT (principal_id, kind) DO UPDATE"
    " SET data = EXCLUDED.data,"
    " version = user_workspaces.version + 1,"
    " updated_at = now()"
    " WHERE user_workspaces.version = %s"
    " RETURNING version, updated_at")


def _write(principal_id: str, kind: str, text: str,
           base_version: int) -> dict[str, Any] | None:
    """Store `text` (JSON) when the stored version is `base_version`.

    One statement. A new row starts at version 1, so it is only written when
    there was no row. An existing row is only updated when its version is
    `base_version`. Returns None when another write came first.
    """
    with db.transaction() as cur:
        cur.execute(_UPSERT_SQL, (principal_id, kind, text, base_version))
        row: dict[str, Any] | None = cur.fetchone()
    return row


def _delete(principal_id: str, kind: str) -> None:
    db.execute(
        "DELETE FROM user_workspaces WHERE principal_id = %s AND kind = %s",
        (principal_id, kind))


# ---- helpers -----------------------------------------------------------------

def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None if value is None else str(value)


def _principal(claims: dict) -> str:
    return str(claims["sub"])


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise deps._error(404, "not_found", "Not found.")


def _check_enabled() -> None:
    if not _enabled():
        raise deps._error(404, "disabled",
                          "The server does not keep workspaces. "
                          "Your browser keeps them.")


def _conflict(row: dict[str, Any] | None) -> JSONResponse:
    """409 with the stored copy, so the client can merge or replace."""
    version = int(row["version"]) if row else 0
    data = row["data"] if row else None
    updated = _iso(row["updated_at"]) if row else None
    return JSONResponse(status_code=409, content={
        "version": version,
        "data": data,
        "updatedAt": updated,
        "error": {"code": "version_conflict",
                  "message": "Another browser saved a newer copy. "
                             "Load it, then save again."},
    })


# ---- routes ------------------------------------------------------------------

@router.get("/{kind}", response_model=None)
def workspace_get(kind: str,
                  claims: dict = Depends(deps.current_user)) -> dict[str, Any]:
    _check_kind(kind)
    if not _enabled():
        return {"enabled": False}
    row = _read(_principal(claims), kind)
    if row is None:
        return {"enabled": True, "data": None, "version": 0}
    return {"enabled": True, "data": row["data"], "version": int(row["version"]),
            "updatedAt": _iso(row["updated_at"])}


@router.put("/{kind}", response_model=None)
def workspace_put(kind: str,
                  claims: dict = Depends(deps.current_user),
                  body: WorkspaceIn = Body(...)) -> dict[str, Any] | JSONResponse:
    _check_kind(kind)
    _check_enabled()
    if not isinstance(body.data, dict):
        raise deps._error(400, "bad_data",
                          "Refused: data must be a JSON object.")
    text = json.dumps(body.data, ensure_ascii=False, separators=(",", ":"))
    limit = _max_bytes()
    if len(text.encode("utf-8")) > limit:
        raise deps._error(413, "too_large",
                          f"Refused: the workspace is larger than "
                          f"{limit // 1024} KB.")
    principal = _principal(claims)
    current = _read(principal, kind)
    stored = int(current["version"]) if current else 0
    if body.baseVersion != stored:
        return _conflict(current)
    row = _write(principal, kind, text, body.baseVersion)
    if row is None:
        # Another write came between the read and the write.
        return _conflict(_read(principal, kind))
    return {"enabled": True, "version": int(row["version"]),
            "updatedAt": _iso(row["updated_at"])}


@router.delete("/{kind}", status_code=204, response_model=None)
def workspace_delete(kind: str,
                     claims: dict = Depends(deps.current_user)) -> Response:
    _check_kind(kind)
    # No switch check: forgetting the stored copy works while sync is off.
    _delete(_principal(claims), kind)
    return Response(status_code=204)
