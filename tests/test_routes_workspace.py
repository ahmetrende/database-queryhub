"""A person's workspace and UI preferences on the server (migration 221).

The routes get a dict in place of `user_workspaces`. The dict keeps the same
version rule as the SQL upsert: a new row starts at 1, and an update only
happens when the stored version is the base version.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from queryhub.web import routes_workspace as rw

ME, OTHER = "U0WSPACE01", "U0WSPACE02"
WHEN = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    settings = {"workspace_sync_enabled": "on", "workspace_max_kb": "1"}

    def get_setting(key: str, default: str | None = None) -> str:
        value = settings.get(key, default)
        assert value is not None
        return value

    monkeypatch.setattr(rw.cfg, "get_setting", get_setting)

    def read(pid: str, kind: str) -> dict[str, Any] | None:
        row = rows.get((pid, kind))
        return dict(row) if row else None

    def write(pid: str, kind: str, text: str, base: int) -> dict[str, Any] | None:
        import json
        row = rows.get((pid, kind))
        if row is None:
            rows[(pid, kind)] = {"data": json.loads(text), "version": 1, "updated_at": WHEN}
        elif row["version"] == base:
            row.update(data=json.loads(text), version=row["version"] + 1)
        else:
            return None
        return {"version": rows[(pid, kind)]["version"], "updated_at": WHEN}

    def delete(pid: str, kind: str) -> None:
        rows.pop((pid, kind), None)

    monkeypatch.setattr(rw, "_read", read)
    monkeypatch.setattr(rw, "_write", write)
    monkeypatch.setattr(rw, "_delete", delete)
    return rows


def _claims(uid: str = ME) -> dict[str, Any]:
    return {"sub": uid, "name": "me"}


def _call(fn: Callable[..., Any], *a: Any, uid: str = ME) -> Any:
    try:
        return fn(*a, _claims(uid))
    except HTTPException as e:
        return (e.status_code, e.detail["code"])


def _put(kind: str, data: Any, base: int, uid: str = ME) -> Any:
    try:
        return rw.workspace_put(kind, _claims(uid), rw.WorkspaceIn(data=data, baseVersion=base))
    except HTTPException as e:
        return (e.status_code, e.detail["code"])


def _body(resp: JSONResponse) -> dict[str, Any]:
    import json
    return json.loads(bytes(resp.body))


def test_get_without_a_row_gives_version_0(store: dict) -> None:
    assert _call(rw.workspace_get, "workspace") == {"enabled": True, "data": None, "version": 0}


def test_put_on_base_0_stores_version_1_and_get_reads_it(store: dict) -> None:
    out = _put("workspace", {"tabs": [1]}, 0)
    assert out["version"] == 1 and out["updatedAt"] == WHEN.isoformat()
    got = _call(rw.workspace_get, "workspace")
    assert got == {"enabled": True, "data": {"tabs": [1]}, "version": 1,
                   "updatedAt": WHEN.isoformat()}
    assert _put("workspace", {"tabs": [2]}, 1)["version"] == 2


def test_put_with_an_old_base_answers_409_with_the_stored_copy(store: dict) -> None:
    _put("prefs", {"theme": "dark"}, 0)
    _put("prefs", {"theme": "light"}, 1)
    resp = _put("prefs", {"theme": "blue"}, 1)
    assert isinstance(resp, JSONResponse) and resp.status_code == 409
    body = _body(resp)
    assert (body["version"], body["data"]) == (2, {"theme": "light"})
    assert body["updatedAt"] == WHEN.isoformat()
    assert body["error"]["code"] == "version_conflict"
    assert store[(ME, "prefs")]["data"] == {"theme": "light"}


def test_put_on_base_1_without_a_row_answers_409_version_0(store: dict) -> None:
    resp = _put("prefs", {"a": 1}, 1)
    assert isinstance(resp, JSONResponse) and resp.status_code == 409
    assert (_body(resp)["version"], _body(resp)["data"]) == (0, None)


def test_a_lost_race_answers_409_with_the_current_row(store: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _put("workspace", {"tabs": []}, 0)
    monkeypatch.setattr(rw, "_write", lambda *a: None)
    resp = _put("workspace", {"tabs": [9]}, 1)
    assert isinstance(resp, JSONResponse) and resp.status_code == 409
    assert _body(resp)["version"] == 1


def test_put_too_large_answers_413(store: dict) -> None:
    # workspace_max_kb is 1 in the fixture.
    assert _put("workspace", {"sql": "x" * 1100}, 0) == (413, "too_large")
    assert (ME, "workspace") not in store
    assert _put("workspace", {"sql": "x" * 900}, 0)["version"] == 1


@pytest.mark.parametrize("data", [[1, 2], "text", 5, None])
def test_put_data_that_is_not_an_object_answers_400(store: dict, data: Any) -> None:
    assert _put("workspace", data, 0) == (400, "bad_data")


def test_unknown_kind_answers_404(store: dict) -> None:
    assert _call(rw.workspace_get, "secrets") == (404, "not_found")
    assert _put("secrets", {}, 0) == (404, "not_found")
    assert _call(rw.workspace_delete, "secrets") == (404, "not_found")


def test_sync_off_get_says_disabled_and_put_answers_404(store: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rw.cfg, "get_setting",
                        lambda key, default=None: "off" if key == "workspace_sync_enabled" else default)
    assert _call(rw.workspace_get, "workspace") == {"enabled": False}
    assert _put("workspace", {"a": 1}, 0) == (404, "disabled")
    assert store == {}


def test_delete_removes_only_the_callers_row(store: dict) -> None:
    _put("workspace", {"mine": True}, 0)
    _put("prefs", {"mine": True}, 0)
    _put("workspace", {"theirs": True}, 0, uid=OTHER)
    resp = _call(rw.workspace_delete, "workspace")
    assert resp.status_code == 204
    assert set(store) == {(ME, "prefs"), (OTHER, "workspace")}


def test_two_principals_do_not_see_each_others_rows(store: dict) -> None:
    _put("workspace", {"owner": "me"}, 0)
    assert _call(rw.workspace_get, "workspace", uid=OTHER)["version"] == 0
    # The other person's first write starts their own row at version 1.
    assert _put("workspace", {"owner": "other"}, 0, uid=OTHER)["version"] == 1
    assert _call(rw.workspace_get, "workspace")["data"] == {"owner": "me"}
    assert _call(rw.workspace_get, "workspace", uid=OTHER)["data"] == {"owner": "other"}


def test_the_upsert_is_one_statement_guarded_by_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Cur:
        def execute(self, sql: str, params: tuple) -> None:
            seen["sql"], seen["params"] = sql, params

        def fetchone(self) -> dict[str, Any] | None:
            return None

    class Tx:
        def __enter__(self) -> Cur:
            return Cur()

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(rw.db, "transaction", lambda: Tx())
    assert rw._write(ME, "prefs", '{"a":1}', 3) is None
    assert "WHERE user_workspaces.version = %s" in seen["sql"]
    assert "RETURNING version, updated_at" in seen["sql"]
    assert seen["params"] == (ME, "prefs", '{"a":1}', 3)


def test_the_router_serves_one_path_under_api_me() -> None:
    paths = {r.path for r in rw.router.routes}  # type: ignore[attr-defined]
    assert paths == {"/api/me/workspace/{kind}"}
