"""Deployed build identity for the web UI.

The backend owns the build stamp — it knows the exact git commit it runs out
of — so nothing needs manual bumping and it survives a prototype refresh (the
prototype's hardcoded QH_VERSION / QH_BUILD are overridden client-side by what
the `/` route injects).

`version()` returns the compact login-footer stamp (the commit datetime in the
configured display timezone), matching the prototype's QH_VERSION slot. `build()` returns the richer
identity the build stamp + What's-new page render: {version, date, sha, branch,
repo}. Both reflect the current HEAD, so they update on every commit without a
restart (frontend-only changes don't restart the web service). Cached briefly
so a burst of page loads doesn't re-shell git; empty strings if git is
unavailable (the client then falls back to the constants in the prototype).
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

# src/queryhub/web/build_info.py → parents[3] is the repo root.
_ROOT = str(Path(__file__).resolve().parents[3])
_TTL = 30.0
_bcache: dict = {"v": None, "t": 0.0}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", _ROOT, *args],
        capture_output=True, text=True, timeout=2,
        # Pin git's own idea of local time to UTC so nothing here depends on
        # the host's TZ. The stamp is then converted explicitly, once, in
        # _commit_date().
        env={**os.environ, "TZ": "UTC"},
    ).stdout.strip()


def _commit_date() -> str:
    """HEAD's commit time as "YYYY-MM-DD HH:MM" in the configured display
    timezone.

    This used to ask git for `--date=format-local` under TZ=UTC, so the stamp
    was UTC while the docstring here claimed TR time — three hours off for the
    people reading it, and nothing in the UI said which zone it was. Take the
    commit's epoch seconds (unambiguous) and render them through the same
    `web_display_timezone` the rest of the web UI formats timestamps with, so
    the build stamp agrees with every other time on the screen.
    """
    ct = _git("show", "-s", "--format=%ct", "HEAD")
    if not ct.isdigit():
        return ""
    from .mapping import _display_tz
    return datetime.fromtimestamp(int(ct), _display_tz()).strftime("%Y-%m-%d %H:%M")


def version() -> str:
    """Compact login-footer stamp (design's QH_VERSION slot): the HEAD commit
    datetime in the display timezone. The richer identity lives in build()."""
    return build().get("date", "")


def build() -> dict:
    """Build metadata for the build stamp + What's-new page:
    {version, date, sha, branch, repo}.

    - version: "r<N>" where N = commit count on HEAD — a real, monotonic build
      number. This repo carries no release tags, so a semver would be fiction;
      the revision count is honest and fills the design's version slot as a
      value distinct from date/sha.
    - date: HEAD commit datetime in `web_display_timezone`,
      "YYYY-MM-DD HH:MM".
    - sha: short HEAD hash.
    - branch: current branch.
    - repo: GitHub slug from bot_config (`web_repo_slug`; may be "" — the UI
      then shows SHAs as plain text, without a commit link).

    Cached briefly so page loads don't re-shell git."""
    now = time.monotonic()
    if _bcache["v"] is not None and now - _bcache["t"] < _TTL:
        return _bcache["v"]
    try:
        short = _git("rev-parse", "--short", "HEAD")
        when = _commit_date()
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        count = _git("rev-list", "--count", "HEAD")
        # Is the running tree actually THIS commit? A deployment serving
        # uncommitted edits would otherwise present a clean commit identity —
        # the stamp says "r369 · 5d4a719" while the code on disk is something
        # nobody can look up. Mark it instead of quietly lying; the marker
        # disappears the moment the work is committed.
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
        from ..config import get_setting
        repo = (get_setting("web_repo_slug", "") or "").strip()
        b = {"version": ("r" + count) if count else "",
             "date": when, "sha": (short + "+dirty") if dirty else short,
             "branch": branch, "repo": repo, "dirty": dirty}
    except Exception:
        b = {"version": "", "date": "", "sha": "", "branch": "", "repo": "",
             "dirty": False}
    _bcache["v"], _bcache["t"] = b, now
    return b
