"""The in-app "What's new" feed.

`GET /api/changelog` renders a curated `changelog.json`. The file is not
generated from git history. A commit log records changes to the code. This page
says what changed for the person who uses the app: one entry per user-facing
feature, written for them.

Where the file is read from: bot_config `web_changelog_path` if set, otherwise
`site/changelog.json` next to the checkout. The app reads it again when its
mtime changes, so a new entry shows on the next page load with no restart. A
missing file is not an error: the page shows no entries.

The file is bilingual. English is the default language. A request can ask for
another language (`?lang=tr`). An entry with no text in that language falls
back to English. Labels in other languages (category names, the page's own UI
strings) live in the file under `i18n.<lang>`, because this repository holds
English only.

Entry shape (version 2):

    {"id", "d": "YYYY-MM-DD", "c": <category key>, "a": "user" | "admin",
     "brk": true (optional), "img": {"before": <file>, "after": <file>} (optional),
     "en": {"t": title, "s": tl;dr summary, "act": action (with brk),
            "alt": {"before", "after"} (with img),
            "p": [{"k": new|improved|fixed|changed, "h": result line, "d": detail}]},
     "tr": {...same keys...}}

Older entries used `a` = requester / approver / both / everyone and bullets
`{"k", "t": "**Lead.** detail"}`. Both still read: `approver` is `admin`,
everything else is `user`, and a bold lead becomes the result line.

Admin entries go to admins only. The decision is made here, on the server: a
person who is not an admin never receives them.

Screenshots live in `docs/whatsnew/` in this repository. They show mock data
only. `GET /api/changelog/media/<file>` serves them, and the GitHub CHANGELOG
shows the same files.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# src/queryhub/web/changelog.py -> parents[3] is the repo root.
_ROOT = Path(__file__).resolve().parents[3]

# Screenshots for entries. Mock data only, so they are safe in the repo and in
# the open-source export.
MEDIA_DIR = _ROOT / "docs" / "whatsnew"
_MEDIA_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}\.(png|webp|gif|jpg)$")
_MEDIA_TYPES = {"png": "image/png", "webp": "image/webp", "gif": "image/gif",
                "jpg": "image/jpeg"}

DEFAULT_LANG = "en"

# English label per language-neutral category key. Other languages read their
# labels from the file (`i18n.<lang>.categories`).
_LABEL = {
    "web": "Web UI",
    "query": "Query and results",
    "batch": "Batch",
    "access": "Access and approvals",
    "safety": "Security and PII",
    "schema": "Schema and discovery",
    "notif": "Notifications",
    "import": "Import",
    "fav": "Templates and favorites",
    "milestone": "Milestone",
}

_KINDS = {"new", "improved", "fixed", "changed"}
_LEAD = re.compile(r"^\s*\*\*(.+?)\*\*\s*(.*)$", re.S)

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


def _load() -> dict:
    """The whole file, cached until it changes on disk. `{}` when missing."""
    path = _source_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if (_cache["v"] is not None and _cache["mtime"] == mtime
            and _cache["path"] == str(path)):
        return _cache["v"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {"entries": data if isinstance(data, list) else []}
    except Exception:
        data = {}
    _cache.update(v=data, mtime=mtime, path=str(path))
    return data


def _entries() -> list[dict]:
    """Curated entries, newest first."""
    return sorted(_load().get("entries", []) or [],
                  key=lambda e: (e.get("d", ""), e.get("id", "")), reverse=True)


def audience(entry: dict) -> str:
    """`admin` or `user`. Reads the old values too."""
    return "admin" if entry.get("a") in ("admin", "approver") else "user"


def _languages(data: dict) -> list[str]:
    """Languages the file has text for. English always comes first."""
    seen = [DEFAULT_LANG]
    for e in data.get("entries", []) or []:
        for key, side in e.items():
            if (isinstance(side, dict) and len(key) == 2 and key.isalpha()
                    and side.get("t") and key not in seen):
                seen.append(key)
    return seen


def pick_lang(requested: str | None, data: dict | None = None) -> str:
    """The language to serve: the requested one when the file has it."""
    lang = (requested or "").strip().lower()[:2]
    if lang and lang in _languages(data if data is not None else _load()):
        return lang
    return DEFAULT_LANG


def _point(pt) -> dict | None:
    """One bullet as {type, text, detail}. Reads the old `**Lead.** rest`."""
    if isinstance(pt, str):
        pt = {"k": "changed", "t": pt}
    if not isinstance(pt, dict):
        return None
    kind = str(pt.get("k", "changed")).lower()
    head, detail = (pt.get("h") or "").strip(), (pt.get("d") or "").strip()
    if not head:
        old = (pt.get("t") or "").strip()
        m = _LEAD.match(old)
        if m:
            head, detail = m.group(1).strip().rstrip("."), m.group(2).strip()
        else:
            head = old.replace("**", "")
    if not head:
        return None
    return {"type": kind if kind in _KINDS else "changed", "text": head,
            "detail": detail}


def _shots(entry: dict, side: dict, base: dict) -> dict | None:
    """Before and after file names with their alt text, or None."""
    img = entry.get("img")
    if not isinstance(img, dict):
        return None
    alt = side.get("alt") or base.get("alt") or {}
    out = {}
    for which in ("before", "after"):
        name = img.get(which)
        if isinstance(name, str) and _MEDIA_NAME.match(name):
            out[which] = name
            out[which + "Alt"] = (alt.get(which) or "").strip()
    return out or None


def releases(for_admin: bool = False, lang: str = DEFAULT_LANG) -> list[dict]:
    """Curated entries (newest first) shaped for the What's-new page.

    `for_admin` includes the admin entries. A person who is not an admin never
    receives them. `lang` picks the text. A field with no text in that language
    falls back to English.
    """
    data = _load()
    labels = ((data.get("i18n") or {}).get(lang) or {}).get("categories") or {}
    rels: list[dict] = []
    for e in _entries():
        aud = audience(e)
        if aud == "admin" and not for_admin:
            continue
        base = e.get(DEFAULT_LANG) or {}
        side = base if lang == DEFAULT_LANG else (e.get(lang) or {})
        text = side if side.get("t") else base
        raw_points = text.get("p") or []
        points = [p for p in (_point(x) for x in raw_points) if p]
        cat = e.get("c", "")
        breaking = bool(e.get("brk"))
        rels.append({
            "id": e.get("id", ""),
            "date": e.get("d", ""),
            "category": cat,
            "area": labels.get(cat) or _LABEL.get(cat, cat),
            "audience": aud,
            "breaking": breaking,
            "action": (text.get("act") or "").strip() if breaking else "",
            "headline": text.get("t", ""),
            "summary": text.get("s", ""),
            "changes": points,
            "shots": _shots(e, text, base),
            # Kept for clients built before version 2. Always empty.
            "version": "",
            "sha": "",
            "commits": [],
        })
    return rels


def feed(for_admin: bool, requested_lang: str | None) -> dict:
    """The full `GET /api/changelog` body."""
    data = _load()
    lang = pick_lang(requested_lang, data)
    i18n = (data.get("i18n") or {}).get(lang) or {}
    return {
        "lang": lang,
        "languages": _languages(data),
        # UI strings for the page in this language. Empty for English: the
        # page carries its English strings itself.
        "ui": {} if lang == DEFAULT_LANG else (i18n.get("ui") or {}),
        "isAdmin": bool(for_admin),
        "releases": releases(for_admin=for_admin, lang=lang),
    }


def media_file(name: str) -> tuple[Path, str] | None:
    """(path, media type) of one screenshot, or None for any name that is not
    a plain file name in MEDIA_DIR."""
    m = _MEDIA_NAME.match(name or "")
    if not m:
        return None
    path = MEDIA_DIR / name
    try:
        if path.resolve().parent != MEDIA_DIR.resolve() or not path.is_file():
            return None
    except OSError:
        return None
    return path, _MEDIA_TYPES[m.group(1)]
