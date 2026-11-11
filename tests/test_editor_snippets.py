"""Editor snippets (Devart D3 and D4): the value rules and the four routes.

The SQL itself runs in tests/test_integration_editor_snippets.py. Here the
routes get fakes: what they send to `snippets`, and what they answer.
"""
import pytest
from fastapi import HTTPException

from queryhub import snippets
from queryhub.web import routes_data as rd

ME, OTHER = "U0SNIPME01", "U0SNIPOT01"


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------

def test_clean_gives_the_stored_form():
    v = snippets.clean(key=" TopN ", description="  Top   rows ", body="SELECT 1\r\nFROM t",
                       engine=" MSSQL ", folder="  My   reports ", is_shared=1)
    assert v == {"key": "topn", "description": "Top rows", "body": "SELECT 1\nFROM t",
                 "engine": "mssql", "folder": "My reports", "is_shared": True}
    empty = snippets.clean(key="ab", body="x", description=" ", engine="", folder=None)
    assert (empty["description"], empty["engine"], empty["folder"], empty["is_shared"]) == (None, None, None, False)


@pytest.mark.parametrize("kw, code", [
    ({"key": "a"}, "bad_key"),
    ({"key": "1ab"}, "bad_key"),
    ({"key": "a-b"}, "bad_key"),
    ({"key": "a" * 33}, "bad_key"),
    ({"description": "d" * 201}, "bad_description"),
    ({"body": "  \n "}, "bad_body"),
    ({"body": "x" * 20_001}, "bad_body"),
    ({"engine": "oracle"}, "bad_engine"),
    ({"folder": "f" * 65}, "bad_folder"),
])
def test_clean_refuses_what_the_table_refuses(kw, code):
    args = {"key": "topn", "body": "SELECT 1", **kw}
    with pytest.raises(snippets.SnippetError) as e:
        snippets.clean(**args)
    assert e.value.code == code


def test_the_limits_at_their_edge_pass():
    v = snippets.clean(key="a" + "b" * 31, description="d" * 200, body="x" * 20_000, folder="f" * 64)
    assert len(v["key"]) == 32


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

ROWS = [
    {"id": 1, "owner_id": ME, "key": "topn", "description": None, "body": "SELECT 1",
     "engine": None, "folder": "R", "is_shared": False, "created_at": None, "updated_at": None},
    {"id": 2, "owner_id": OTHER, "key": "lastday", "description": "d", "body": "SELECT 2",
     "engine": "postgres", "folder": "X", "is_shared": True, "created_at": None, "updated_at": None},
]


@pytest.fixture
def api(monkeypatch):
    calls = {}
    monkeypatch.setattr(rd.deps, "require_whitelisted", lambda claims: None)
    monkeypatch.setattr(rd, "_is_admin", lambda uid: uid == "U0SNIPADM1")
    monkeypatch.setattr(rd.people, "display_names", lambda keys: calls.setdefault("names", set(keys)) and {OTHER: "Kim"})
    monkeypatch.setattr(snippets, "list_for", lambda uid: calls.setdefault("list", uid) and ROWS)

    def create(uid, values):
        calls["create"] = (uid, values)
        if values["key"] == "dupe":
            raise snippets.SnippetError("duplicate_key", "You have a snippet with the key dupe.")
        return {**ROWS[0], **values, "owner_id": uid}
    monkeypatch.setattr(snippets, "create", create)

    def update(uid, sid, values):
        calls["update"] = (uid, sid, values)
        return None if sid == 404 else {**ROWS[0], **values, "id": sid, "owner_id": uid}
    monkeypatch.setattr(snippets, "update", update)

    def delete(uid, sid, *, is_admin):
        calls["delete"] = (uid, sid, is_admin)
        return sid != 404
    monkeypatch.setattr(snippets, "delete", delete)
    return calls


def _call(fn, *a, uid=ME):
    try:
        return fn(*a, claims={"sub": uid, "name": "me"})
    except HTTPException as e:
        return (e.status_code, e.detail["code"])


def test_list_marks_own_rows_and_names_only_other_owners(api):
    out = rd.snippets_list(claims={"sub": ME})
    assert api["list"] == ME
    assert api["names"] == {OTHER}
    mine, other = out["snippets"]
    assert (mine["mine"], mine["owner"], mine["id"]) == (True, None, "1")
    assert (other["mine"], other["owner"], other["shared"]) == (False, "Kim", True)
    assert all("owner_id" not in s for s in out["snippets"]), "the principal id stays on the server"
    assert out["canDeleteShared"] is False
    assert rd.snippets_list(claims={"sub": "U0SNIPADM1"})["canDeleteShared"] is True


def test_create_cleans_first_and_maps_errors(api):
    body = rd.SnippetIn(key="TopN", body="SELECT 1", shared=True)
    out = _call(rd.snippets_create, body)
    assert api["create"][0] == ME
    assert api["create"][1]["key"] == "topn"
    assert (out["key"], out["shared"], out["mine"]) == ("topn", True, True)
    api.pop("create")
    assert _call(rd.snippets_create, rd.SnippetIn(key="x", body="SELECT 1")) == (422, "bad_key")
    assert "create" not in api, "a bad value never reaches the database"
    assert _call(rd.snippets_create, rd.SnippetIn(key="dupe", body="SELECT 1")) == (409, "duplicate_key")


def test_update_and_delete_answer_404_for_a_snippet_that_is_not_yours(api):
    body = rd.SnippetIn(key="topn", body="SELECT 3")
    assert _call(rd.snippets_update, 404, body) == (404, "not_found")
    out = _call(rd.snippets_update, 7, body)
    assert (out["id"], out["body"]) == ("7", "SELECT 3")
    assert api["update"][:2] == (ME, 7)
    assert _call(rd.snippets_update, 7, rd.SnippetIn(key="topn", body=" ")) == (422, "bad_body")
    assert _call(rd.snippets_delete, 404) == (404, "not_found")
    assert _call(rd.snippets_delete, 8) is None
    assert api["delete"] == (ME, 8, False)
    _call(rd.snippets_delete, 9, uid="U0SNIPADM1")
    assert api["delete"] == ("U0SNIPADM1", 9, True), "an admin may delete a shared one"


def test_the_route_limits_are_wider_than_the_rules():
    """A too-long value reaches clean() and gets its own message, not a bare
    pydantic error, unless it is far too long."""
    assert rd.SnippetIn.model_fields["key"].metadata[0].max_length > 32
    assert rd.SnippetIn.model_fields["body"].metadata[0].max_length > snippets.MAX_BODY
