"""Editor snippets of a person (2026-11-11, Devart D3 and D4).

Schema: `migrations/205_editor_snippets.sql`. A snippet is a short key and a
SQL body. The web editor offers it in the typing list, as it offers the
built-in snippets (`ssf`, `cte`). A person sees their own snippets and the
shared snippets of every other person. Only the owner changes a snippet. The
owner or an admin deletes it: an admin can delete a shared snippet that
should not be there.

`clean` holds the same limits as the table checks, so a bad value gets a
clear message and not a database error.
"""
from __future__ import annotations

import re

import psycopg

from . import db

ENGINES = ("postgres", "mssql", "clickhouse", "athena")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
MAX_DESCRIPTION = 200
MAX_BODY = 20_000
MAX_FOLDER = 64
# A bound per owner keeps the typing list and the panel usable.
MAX_PER_OWNER = 500

_COLS = ("id, owner_id, key, description, body, engine, folder, is_shared, "
         "created_at, updated_at")


class SnippetError(ValueError):
    """A value or a state that the API refuses. `code` names it."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def clean(*, key, description=None, body, engine=None, folder=None,
          is_shared=False) -> dict:
    """The stored form of one snippet, or SnippetError.

    The key is lower case. Spaces in the description and the folder become
    one space each. An empty description, engine or folder is NULL. The body
    keeps its text, with Windows line ends made plain."""
    k = str(key or "").strip().lower()
    if not KEY_RE.match(k):
        raise SnippetError(
            "bad_key",
            "Use 2 to 32 characters for the key: a letter first, then "
            "letters, digits or _.")
    d = " ".join(str(description or "").split()) or None
    if d is not None and len(d) > MAX_DESCRIPTION:
        raise SnippetError(
            "bad_description",
            f"Use at most {MAX_DESCRIPTION} characters for the description.")
    b = str(body or "").replace("\r\n", "\n")
    if not b.strip():
        raise SnippetError("bad_body", "Write the SQL of the snippet.")
    if len(b) > MAX_BODY:
        raise SnippetError(
            "bad_body", f"Use at most {MAX_BODY} characters for the SQL.")
    e = str(engine or "").strip().lower() or None
    if e is not None and e not in ENGINES:
        raise SnippetError("bad_engine", "Select a known engine, or all engines.")
    f = " ".join(str(folder or "").split()) or None
    if f is not None and len(f) > MAX_FOLDER:
        raise SnippetError(
            "bad_folder", f"Use at most {MAX_FOLDER} characters for the folder.")
    return {"key": k, "description": d, "body": b, "engine": e,
            "folder": f, "is_shared": bool(is_shared)}


def list_for(principal_id: str) -> list[dict]:
    """The person's own snippets and every shared one, in key order."""
    return db.fetch_all(
        f"SELECT {_COLS} FROM editor_snippets "
        " WHERE owner_id = %s OR is_shared "
        " ORDER BY key, owner_id <> %s, id",
        (principal_id, principal_id))


def _duplicate(key: str) -> SnippetError:
    return SnippetError("duplicate_key",
                        f"You have a snippet with the key {key}. Use a different key.")


def create(principal_id: str, values: dict) -> dict:
    """Insert one snippet for its owner. `values` comes from `clean`."""
    try:
        with db.transaction() as cur:
            cur.execute("SELECT count(*) AS n FROM editor_snippets WHERE owner_id = %s",
                        (principal_id,))
            if cur.fetchone()["n"] >= MAX_PER_OWNER:
                raise SnippetError(
                    "too_many",
                    f"You have {MAX_PER_OWNER} snippets. Delete one before you add one.")
            cur.execute(
                "INSERT INTO editor_snippets "
                "  (owner_id, key, description, body, engine, folder, is_shared) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
                (principal_id, values["key"], values["description"], values["body"],
                 values["engine"], values["folder"], values["is_shared"]))
            return cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise _duplicate(values["key"]) from None


def update(principal_id: str, snippet_id: int, values: dict) -> dict | None:
    """Change the owner's snippet. None when the person does not own it, or
    when it does not exist: the two answers are the same."""
    try:
        with db.transaction() as cur:
            cur.execute(
                "UPDATE editor_snippets "
                "   SET key = %s, description = %s, body = %s, engine = %s, "
                "       folder = %s, is_shared = %s, updated_at = NOW() "
                f" WHERE id = %s AND owner_id = %s RETURNING {_COLS}",
                (values["key"], values["description"], values["body"],
                 values["engine"], values["folder"], values["is_shared"],
                 snippet_id, principal_id))
            return cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise _duplicate(values["key"]) from None


def delete(principal_id: str, snippet_id: int, *, is_admin: bool) -> bool:
    """Delete the owner's snippet. An admin also deletes a shared one."""
    with db.transaction() as cur:
        cur.execute(
            "DELETE FROM editor_snippets "
            " WHERE id = %s AND (owner_id = %s OR (%s AND is_shared)) "
            "RETURNING id",
            (snippet_id, principal_id, bool(is_admin)))
        return cur.fetchone() is not None
