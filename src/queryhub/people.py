"""One answer to "who is this principal", for every payload that prints a person.

A person reaches the API under three different shapes and the screens were
printing all three raw:

* a **Slack id** (`U0…`) — the masking rows store this as `created_by`;
* a **handle** (`first.last`) — `requests.requester_name` holds one wherever the
  identity source had no display name, which is 2,275 of the last 6,073
  requests;
* a **display name**, which is what every screen wanted in the first place —
  except that half the stored ones are Slack's ASCII fold of it, so the same
  colleague reaches a ranked list twice, once per spelling.

The design side added a helper that title-cases a handle so the queue reads as
people rather than logins. It is deliberately narrow and it says so: it cannot
recover a diacritic (`sahin` stays `Sahin`), and it refuses a role-prefixed
handle outright, because `dba.ops` names a person but `Dba Ops` is a surname
nobody has. This module is the other half — the one that can actually look the
person up — and where it answers, the helper has nothing left to do.

Resolution is deliberately cheap and total:

* an id matches `requesters.slack_user_id` / `admins.slack_user_id`;
* a handle matches the local part of the stored email, case-insensitively —
  `ilker.sahin` is `ilker.sahin@…`, which is how these handles were minted;
* a name matches a roster name with the same letters once diacritics are
  folded away, and only if exactly one roster name folds to it — the fold is
  lossy, so an ambiguous one is left alone rather than guessed at;
* anything else, including a `dba.*` service principal that belongs to no
  person, comes back unchanged. Returning the input is the honest answer and
  the screens already render it.

Both tables, because an admin who never submitted a query has no `requesters`
row — the same reason the audit trail's lookup reads both.
"""
from __future__ import annotations

import re
import unicodedata

from . import db

# `U…`/`W…` ids are what Slack mints; anything else that carries a dot or an
# underscore and no space is a handle. A bare word is neither, and is left alone.
_SLACK_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")
_HANDLE = re.compile(r"^[A-Za-z0-9._-]+$")


def _kind(key: str) -> str:
    if _SLACK_ID.match(key):
        return "id"
    if " " in key:
        return "name"
    if "." in key or "_" in key:
        return "handle" if _HANDLE.match(key) else "other"
    return "other"


def fold(name: str) -> str:
    """The spelling Slack's `real_name_normalized` would have produced.

    NFKD separates every Turkish diacritic from its letter except the dotless
    lowercase i, which is a letter in its own right and decomposes to nothing --
    so it, and the dotted capital I, are mapped by hand."""
    flat = "".join(c for c in unicodedata.normalize("NFKD", name)
                   if not unicodedata.combining(c))
    return flat.replace("\u0131", "i").replace("\u0130", "i").casefold().strip()


def display_names(keys) -> dict[str, str]:
    """{key: display name} for the keys that resolve. Absent means "no answer" —
    callers keep what they had rather than substituting anything."""
    wanted = {k.strip() for k in keys if isinstance(k, str) and k.strip()}
    if not wanted:
        return {}
    ids = sorted(k for k in wanted if _kind(k) == "id")
    handles = sorted(k for k in wanted if _kind(k) == "handle")
    out: dict[str, str] = {}

    if ids:
        for r in db.fetch_all(
                "SELECT slack_user_id AS k, name FROM admins "
                " WHERE slack_user_id = ANY(%(ids)s) AND name IS NOT NULL "
                "UNION ALL "
                "SELECT slack_user_id, name FROM requesters "
                " WHERE slack_user_id = ANY(%(ids)s) AND name IS NOT NULL",
                {"ids": ids}):
            out.setdefault(r["k"], r["name"])

    if handles:
        # The local part, lowercased on both sides: the handle is how the
        # address is spelled, and neither side is reliably cased.
        lowered = [h.lower() for h in handles]
        by_local: dict[str, str] = {}
        for r in db.fetch_all(
                "SELECT lower(split_part(email, '@', 1)) AS k, name FROM admins "
                " WHERE email IS NOT NULL AND name IS NOT NULL "
                "   AND lower(split_part(email, '@', 1)) = ANY(%(h)s) "
                "UNION ALL "
                "SELECT lower(split_part(email, '@', 1)), name FROM requesters "
                " WHERE email IS NOT NULL AND name IS NOT NULL "
                "   AND lower(split_part(email, '@', 1)) = ANY(%(h)s)",
                {"h": lowered}):
            by_local.setdefault(r["k"], r["name"])
        for h in handles:
            got = by_local.get(h.lower())
            if got:
                out[h] = got

    names = sorted(k for k in wanted if _kind(k) == "name")
    if names:
        # The roster is tens of rows; fold the whole of it once rather than
        # asking the database to fold, which would need an extension it does
        # not have. A fold shared by two colleagues resolves to neither.
        by_fold: dict[str, set] = {}
        for r in db.fetch_all(
                "SELECT name FROM admins WHERE name IS NOT NULL "
                "UNION SELECT name FROM requesters WHERE name IS NOT NULL"):
            by_fold.setdefault(fold(r["name"]), set()).add(r["name"])
        for n in names:
            got = by_fold.get(fold(n))
            if got and len(got) == 1:
                out[n] = next(iter(got))
    return out


def display_name(key) -> str | None:
    """One key. `None` in, `None` out; unresolvable in, the key back."""
    if not isinstance(key, str) or not key.strip():
        return key
    return display_names([key]).get(key.strip(), key)


def namer(keys):
    """A `key -> name` function over one batched lookup.

    Payload builders take this rather than a dict so the fallback lives in one
    place: a key nothing resolved comes back as itself, which is what every
    screen already renders."""
    resolved = display_names(keys)

    def name_of(key):
        if not isinstance(key, str) or not key.strip():
            return key
        return resolved.get(key.strip(), key)
    return name_of
