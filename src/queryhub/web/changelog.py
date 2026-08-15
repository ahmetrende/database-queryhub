"""Developer-facing changelog for the in-app "What's new" page.

`GET /changelog` renders a curated `changelog.json`. The file is deliberately
NOT generated from git history: a commit log is a record of changes to the code,
and this page answers a different question — what changed for the person using
the app. One entry per user-facing feature, written for them.

Where the file is read from: bot_config `web_changelog_path` if set, otherwise
`site/changelog.json` next to the checkout. It is re-read whenever its mtime
changes (see the cache below), so adding an entry shows up on the next page
load with no restart and no publish step. A missing file is not an error — the
page renders empty.

Entries are language-keyed, so a deployment can serve the language its users
read; this endpoint serves the `en` fields.

Each entry maps to one card (newest first): a one-line summary plus short bullet
points, a category chip and the date. There is no per-release commit list and no
commit SHA link unless `web_repo_slug` names a repository to link to.

Two things the entry carries beyond its text:

- `a` (audience): "requester", "approver" or "both". Approver-only items —
  how the decision queue behaves, what an admin screen gained — are noise to
  someone who only submits queries, and the page was showing them to everyone.
  They are now served only to admins.
- `p` (points): the bullet list. Prose paragraphs were the original format and
  they do not get read; a card is scanned. `s` is kept as the one-line lede and
  an entry with no `p` still renders (older entries, or a quick hand-add).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

# src/queryhub/web/changelog.py -> parents[3] is the repo root.
_ROOT = Path(__file__).resolve().parents[3]

# English label per language-neutral category key. A surface rendering another
# language maps the same keys to its own labels. Keep in sync with changelog.json.
_LABEL = {
    "web": "Web UI",
    "query": "Query & results",
    "batch": "Batch",
    "access": "Access & approvals",
    "safety": "Security & PII",
    "schema": "Schema & discovery",
    "notif": "Notifications",
    "import": "Import",
    "fav": "Templates & favorites",
    "milestone": "Milestone",
}

_cache: dict = {"v": None, "mtime": None, "path": None}


def _source_path() -> Path:
    """Curated changelog file. bot_config `web_changelog_path` overrides;
    the default is a `site/` directory alongside the repo checkout."""
    try:
        from ..config import get_setting
        p = (get_setting("web_changelog_path", "") or "").strip()
        if p:
            return Path(p)
    except Exception:
        pass
    return _ROOT.parent / "site" / "changelog.json"


def _fmt_date(d: str) -> str:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
    except Exception:
        return d
    # "Jul 15, 2026" — strip the day's leading zero without %-d (portability).
    return f"{dt:%b} {dt.day}, {dt.year}"


_KINDS = {"new", "improved", "fixed", "changed"}


def _entries() -> list[dict]:
    """Curated entries, newest first, straight from the file. Cached until
    changelog.json changes on disk."""
    path = _source_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    if (_cache["v"] is not None and _cache["mtime"] == mtime
            and _cache["path"] == str(path)):
        return _cache["v"]
    try:
        data = json.load(open(path, encoding="utf-8"))
        entries = sorted(data.get("entries", []),
                         key=lambda e: e.get("d", ""), reverse=True)
    except Exception:
        entries = []
    _cache.update(v=entries, mtime=mtime, path=str(path))
    return entries


def releases(for_approver: bool = False) -> list[dict]:
    """Curated features (newest first) shaped for the What's-new card.

    One card per feature: {version:"", area:<category label>, date:<pretty>,
    sha:"", headline:<title>, summary:<one-line lede>,
    changes:[{type, text}], commits:[]}.

    `for_approver` includes the approver-only entries. Default False, because
    the common caller is someone who submits queries and cannot see an approval
    queue at all — "Approve comes first in the queue" is not news to them.
    """
    rels: list[dict] = []
    for e in _entries():
        if e.get("a") == "approver" and not for_approver:
            continue
        en = e.get("en", {})
        points = []
        for pt in en.get("p", []) or []:
            if isinstance(pt, str):
                points.append({"type": "changed", "text": pt})
                continue
            kind = str(pt.get("k", "changed")).lower()
            points.append({"type": kind if kind in _KINDS else "changed",
                           "text": pt.get("t", "")})
        rels.append({
            "version": "",
            "area": _LABEL.get(e.get("c", ""), e.get("c", "")),
            "date": _fmt_date(e.get("d", "")),
            "sha": "",
            "headline": en.get("t", ""),
            "summary": en.get("s", ""),
            "changes": [p for p in points if p["text"]],
            "commits": [],
            # Sent so the page can default to end-user entries and let an admin
            # opt into the approver ones, rather than deciding for them here.
            "audience": e.get("a", "requester"),
        })
    return rels
