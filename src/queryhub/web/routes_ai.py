"""The AI assistant API (M13): the calls of the editor and the admin form.

    GET    /api/ai/status                  what the editor needs to show it
    POST   /api/ai/complete                one assistant call
    GET    /api/admin/ai                   settings, providers, presets
    PUT    /api/admin/ai                   change settings
    POST   /api/admin/ai/providers         add a provider
    PUT    /api/admin/ai/providers/{id}    change a provider, set or clear its key
    DELETE /api/admin/ai/providers/{id}    delete a provider
    POST   /api/admin/ai/test              a small call to one provider

The rules live in `ai.py`. The admin routes are for a super-admin only: they
hold the API keys. A key goes in through POST or PUT and never comes out. The
answer says only whether a key exists, who set it and when.
"""
from __future__ import annotations

import importlib.util

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import ai, db
from .. import config as cfg
from . import admin, deps

router = APIRouter(prefix="/api", dependencies=[Depends(deps.block_pw_gate)])


def _fail(e: ai.AIError):
    return deps._error(e.status, e.code, e.message)


# --- the editor -----------------------------------------------------------------


class TurnIn(BaseModel):
    role: str
    # Bounds the body. `ai.conversation` cuts each turn to MAX_TURN_CHARS.
    content: str = Field("", max_length=200_000)


class CompleteIn(BaseModel):
    mode: str
    prompt: str = ""
    sql: str = ""
    messages: list[TurnIn] = Field(default_factory=list, max_length=200)
    connectionId: str = ""
    databaseId: str = ""
    model: str | None = None


@router.get("/ai/status")
def ai_status(claims: dict = Depends(deps.current_user)):
    """`enabled` false hides the assistant. Nothing else is said then."""
    deps.require_whitelisted(claims)
    return ai.status()


@router.post("/ai/complete")
def ai_complete(body: CompleteIn, claims: dict = Depends(deps.current_user)):
    deps.require_whitelisted(claims)
    deps.block_if_password_change_required(claims)
    ask = ai.Ask(mode=body.mode, prompt=body.prompt, sql=body.sql,
                 messages=[t.model_dump() for t in body.messages],
                 connection=body.connectionId, database=body.databaseId,
                 model=body.model)
    try:
        return ai.complete(claims["sub"], claims.get("name"), ask)
    except ai.AIError as e:
        raise _fail(e) from e


# --- the admin form (super-admin) ---------------------------------------------------


def _admin_view() -> dict:
    s = ai.settings()
    provs = ai.providers()
    ready, why = ai.readiness(s, provs)
    return {
        "settings": ai.settings_view(s),
        "providers": [ai.provider_view(p) for p in provs],
        "presets": [dict(p) for p in ai.PRESETS],
        "efforts": list(ai.EFFORTS),
        "ready": ready,
        "notReady": why,
        # An Anthropic provider needs the optional `anthropic` package.
        "anthropicSdk": importlib.util.find_spec("anthropic") is not None,
    }


@router.get("/admin/ai")
def admin_ai(claims: dict = Depends(deps.current_user)):
    admin.require_admin(claims, "access")
    return _admin_view()


class SettingsIn(BaseModel):
    settings: dict = Field(default_factory=dict)


@router.put("/admin/ai")
def admin_ai_save(body: SettingsIn, claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    try:
        values = ai.clean_settings(body.settings, ai.providers(), ai.settings())
    except ValueError as e:
        raise deps._error(400, "bad_request", str(e)) from e
    with db.transaction() as cur:
        changed = ai.save_settings(cur, values, uid, claims.get("name"))
    if changed:
        cfg.invalidate_cache()
    return {**_admin_view(), "applied": len(changed)}


class ProviderIn(BaseModel):
    name: str | None = None
    kind: str | None = None
    baseUrl: str | None = None
    models: list[str] | str | None = None
    supportsTools: bool | None = None
    enabled: bool | None = None
    # Write-only. Never echoed, never logged.
    apiKey: str | None = Field(None, max_length=ai.MAX_KEY_CHARS)
    clearKey: bool = False


def _fields(body: ProviderIn) -> dict:
    return body.model_dump(exclude_unset=True, exclude={"apiKey", "clearKey"})


def _key(body: ProviderIn) -> str | None:
    if body.apiKey is None or body.apiKey == "":
        return None
    try:
        return ai.clean_key(body.apiKey)
    except ValueError as e:
        raise deps._error(400, "bad_request", str(e)) from e


def _name_taken() -> Exception:
    return deps._error(409, "conflict", "Another provider has this name.")


@router.post("/admin/ai/providers", status_code=201)
def admin_ai_provider_add(body: ProviderIn, claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    try:
        fields = ai.clean_provider(_fields(body))
    except ValueError as e:
        raise deps._error(400, "bad_request", str(e)) from e
    key = _key(body)
    try:
        with db.transaction() as cur:
            pid = ai.create_provider(cur, fields, key, uid, claims.get("name"))
    except psycopg.errors.UniqueViolation as e:
        raise _name_taken() from e
    return {**_admin_view(), "id": pid}


def _existing(provider_id: int) -> ai.Provider:
    p = ai.get_provider(provider_id)
    if p is None:
        raise deps._error(404, "not_found", "Unknown provider.")
    return p


@router.put("/admin/ai/providers/{provider_id}")
def admin_ai_provider_save(provider_id: int, body: ProviderIn,
                           claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    current = _existing(provider_id)
    try:
        fields = ai.clean_provider(_fields(body), current)
    except ValueError as e:
        raise deps._error(400, "bad_request", str(e)) from e
    key = _key(body)
    if key and body.clearKey:
        raise deps._error(400, "bad_request", "Send a new key or clear the key, not both.")
    try:
        with db.transaction() as cur:
            ai.update_provider(cur, current, fields, key, body.clearKey, uid,
                               claims.get("name"))
    except psycopg.errors.UniqueViolation as e:
        raise _name_taken() from e
    return _admin_view()


@router.delete("/admin/ai/providers/{provider_id}")
def admin_ai_provider_delete(provider_id: int, claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    current = _existing(provider_id)
    if ai.settings().default_provider == current.id:
        raise deps._error(409, "conflict", "This is the default provider. Choose "
                                           "another default provider first.")
    with db.transaction() as cur:
        ai.delete_provider(cur, current, uid, claims.get("name"))
    return _admin_view()


class TestIn(BaseModel):
    providerId: int
    model: str | None = None


@router.post("/admin/ai/test")
def admin_ai_test(body: TestIn, claims: dict = Depends(deps.current_user)):
    uid = admin.require_admin(claims, "access")
    try:
        return ai.test_provider(uid, claims.get("name"), body.providerId, body.model)
    except ai.AIError as e:
        raise _fail(e) from e
