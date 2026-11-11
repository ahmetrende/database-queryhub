"""Editor snippets against a real control DB (migration 205 + snippets.py).

Run with QH_RUN_INTEGRATION=1 and BOT_DB_* pointing at a throwaway database
with every migration applied. The test writes and deletes its own rows only
(owners U0ITSNIP0x).
"""
import os

import psycopg
import pytest

from queryhub import db, snippets

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("QH_RUN_INTEGRATION"),
                       reason="set QH_RUN_INTEGRATION=1 (with a reachable control DB) to run"),
]

A, B, ADMIN = "U0ITSNIP01", "U0ITSNIP02", "U0ITSNIP03"


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM editor_snippets WHERE owner_id = ANY(%s)", ([A, B, ADMIN],))
    yield
    db.execute("DELETE FROM editor_snippets WHERE owner_id = ANY(%s)", ([A, B, ADMIN],))


def _new(owner, key, shared=False, **kw):
    return snippets.create(owner, snippets.clean(key=key, body=kw.pop("body", "SELECT 1"),
                                                 is_shared=shared, **kw))


def _count(owner):
    return db.fetch_one("SELECT count(*) AS n FROM editor_snippets WHERE owner_id = %s", (owner,))["n"]


def test_a_person_sees_their_own_and_every_shared_snippet():
    _new(A, "mine_a")
    _new(B, "priv_b")
    _new(B, "pub_b", shared=True)
    keys = [r["key"] for r in snippets.list_for(A) if r["owner_id"] in (A, B)]
    assert keys == ["mine_a", "pub_b"]


def test_a_second_key_of_one_owner_is_refused_and_nothing_is_written():
    _new(A, "topn")
    with pytest.raises(snippets.SnippetError) as e:
        _new(A, "topn", body="SELECT 2")
    assert e.value.code == "duplicate_key"
    assert _count(A) == 1
    _new(B, "topn")                     # another owner may use the key
    row = _new(A, "other")
    with pytest.raises(snippets.SnippetError) as e:
        snippets.update(A, row["id"], snippets.clean(key="topn", body="x"))
    assert e.value.code == "duplicate_key"


def test_only_the_owner_changes_a_snippet():
    row = _new(A, "topn", shared=True)
    assert snippets.update(B, row["id"], snippets.clean(key="topn", body="DROP TABLE t")) is None
    assert snippets.update(A, 999_999_999, snippets.clean(key="topn", body="x")) is None
    got = snippets.update(A, row["id"], snippets.clean(key="topn2", body="SELECT 9", folder="F"))
    assert (got["key"], got["body"], got["folder"], got["updated_at"] >= got["created_at"]) == ("topn2", "SELECT 9", "F", True)


def test_the_owner_or_an_admin_deletes_and_an_admin_only_a_shared_one():
    shared = _new(A, "pub", shared=True)
    private = _new(A, "priv")
    assert snippets.delete(B, shared["id"], is_admin=False) is False
    assert snippets.delete(ADMIN, private["id"], is_admin=True) is False, "an admin never sees a private one"
    assert snippets.delete(ADMIN, shared["id"], is_admin=True) is True
    assert snippets.delete(A, private["id"], is_admin=False) is True
    assert _count(A) == 0


def test_the_owner_bound(monkeypatch):
    monkeypatch.setattr(snippets, "MAX_PER_OWNER", 2)
    _new(A, "one")
    _new(A, "two")
    with pytest.raises(snippets.SnippetError) as e:
        _new(A, "three")
    assert e.value.code == "too_many"
    assert _count(A) == 2


@pytest.mark.parametrize("col, value", [
    ("key", "A1"), ("key", "x"), ("engine", "oracle"), ("body", ""),
    ("description", "d" * 201), ("folder", ""), ("owner_id", "bob"),
])
def test_the_table_checks_hold_the_same_rules(col, value):
    row = {"owner_id": A, "key": "ok_key", "body": "SELECT 1"}
    row[col] = value
    cols = ", ".join(row)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(f"INSERT INTO editor_snippets ({cols}) VALUES ({', '.join(['%s'] * len(row))})",
                   tuple(row.values()))
