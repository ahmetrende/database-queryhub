#!/usr/bin/env python3
"""Warn when user-facing commits have shipped without a changelog entry.

The What's-new pipeline is fully automatic — a git hook regenerates and
uploads the public page, and the in-app page re-reads the file live — but the
entries themselves are hand-written, and that is the step that gets skipped.
It has: the curated file once sat four days and forty commits behind the code
while every published surface reported itself up to date, because "up to date"
only ever meant "matches the file", never "matches what shipped".

So this compares the two things that were never compared: the newest entry
date in changelog.json against the dates of commits that a user of QueryHub
would notice. It prints what is missing and, by default, EXITS 0 — a missing
release note is not a reason to refuse a push, and a gate that blocks the work
gets removed. `--strict` exits 1 for a caller that does want it to fail (CI, or
a release step).

    python scripts/check_changelog_fresh.py            # warn, always exit 0
    python scripts/check_changelog_fresh.py --strict   # exit 1 when stale
    python scripts/check_changelog_fresh.py --ref HEAD # compare a different ref

Which commits count is a heuristic, and deliberately a loose one: a commit is
user-facing unless every file it touched is on the internal list below (tests,
CI, packaging, docs, operator scripts, migrations). It over-reports rather
than under-reports — the answer to a false positive is one line of judgement
from whoever is pushing, while a false negative is the exact silence this
exists to break.

The changelog itself lives OUTSIDE the repo (it is bilingual, and the repo is
English-only), so this script only ever READS it, and never fails because it
is absent — a checkout without the sibling directory is a normal state.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

# scripts/check_changelog_fresh.py -> parents[1] is the repo root.
_ROOT = Path(__file__).resolve().parents[1]

# A commit whose every path matches one of these is internal: it changes how
# the project is built, tested, packaged, documented or operated, not what a
# requester or an approver sees. Matched with fnmatch against the repo-relative
# path, so a trailing /* means "anything under here".
INTERNAL_PATHS = [
    "tests/*",
    "docs/*",
    "*.md",
    ".github/*",
    ".gitignore",
    ".dockerignore",
    ".gitattributes",
    "MANIFEST.in",
    "Dockerfile*",
    "docker-compose*",
    "pyproject.toml",
    "setup.cfg",
    "requirements*.txt",
    "*.lock",
    "package-lock.json",
    "scripts/*",          # operator tooling, not the product
    "deploy/*",           # unit files, role SQL
    "migrations/*",       # the feature that needs one also changes code
    "QueryHubWeb/app/dist/*",        # build output
    "QueryHubWeb/app/node_modules/*",
    "QueryHubWeb/theme-vendor/*",    # vendored design system
    "QueryHubWeb/_ds/*",
    "*.png",
    "*.jpg",
    "*.svg",
    "*.ico",
]

# Subject prefixes that say "internal" outright. The repo's convention has no
# prefix, so this only catches the few that opt in.
INTERNAL_SUBJECT_PREFIXES = ("docs:", "ci:", "chore:", "test:", "tests:",
                             "refactor:", "style:", "build:")


def _source_path() -> Path:
    """The curated changelog, found the way the app finds it.

    web/changelog.py resolves it as: bot_config `web_changelog_path` if set,
    otherwise a `site/` directory alongside the repo checkout. Ask that module
    directly when it can be imported, so the two cannot drift; fall back to the
    same rule when it cannot — a git hook runs with whatever python3 is on
    PATH, usually without the virtualenv and without the bot's environment.
    """
    try:
        sys.path.insert(0, str(_ROOT / "src"))
        from dba_slack_bot.web import changelog  # noqa: PLC0415
        return changelog._source_path()
    except Exception:
        return _ROOT.parent / "site" / "changelog.json"


def _git(*args: str) -> str:
    return subprocess.run(("git", "-C", str(_ROOT)) + args,
                          capture_output=True, text=True, check=True).stdout


def newest_entry_date(path: Path) -> str | None:
    """YYYY-MM-DD of the most recent entry, or None if unreadable."""
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    dates = [e.get("d", "") for e in data.get("entries", []) if e.get("d")]
    return max(dates) if dates else None


def is_internal(subject: str, files: list[str]) -> bool:
    if subject.lower().startswith(INTERNAL_SUBJECT_PREFIXES):
        return True
    if not files:                      # nothing to judge — don't invent work
        return True
    return all(any(fnmatch.fnmatch(f, pat) for pat in INTERNAL_PATHS)
               for f in files)


def commits_since(ref: str, date: str) -> list[tuple[str, str, str]]:
    """(sha, date, subject) for user-facing commits on `ref` after `date`.

    `--since` is a timestamp, so a commit made later ON the boundary date
    would be included; entries are dated by day, and an entry dated today
    covers work committed today. Filter by date string instead.
    """
    out = _git("log", ref, "--no-merges", "--date=short",
               "--pretty=format:%x00%h%x1f%ad%x1f%s", "--name-only",
               f"--since={date}")
    found = []
    for block in out.split("\x00"):
        if not block.strip():
            continue
        head, _, rest = block.partition("\n")
        sha, cdate, subject = head.split("\x1f", 2)
        if cdate <= date:
            continue
        files = [ln for ln in rest.splitlines() if ln.strip()]
        if not is_internal(subject, files):
            found.append((sha, cdate, subject))
    return found


def report_publishability(path: Path) -> None:
    """Say so, HERE, when the changelog is present but will not publish.

    The S3 page is generated from this file by a `build_whatsnew.py` that sits
    beside it, run by the pre-push hook. That generator refuses an entry whose
    category the page cannot render — correctly — but it was only ever called
    from the publish script, which the hook backgrounded into a log file. So the
    refusal was invisible: four consecutive pushes published nothing while this
    very script printed "up to date" on the line above, because the changelog
    WAS current. Only the generated page was stale.

    So ask the generator, in the foreground, where a person is looking. Its
    `--check` validates and writes nothing. Advisory like the rest of this
    script: a missing or unrunnable generator is normal (an open-source clone has
    no site directory) and must never fail a push.
    """
    gen = path.parent / "build_whatsnew.py"
    if not gen.exists():
        return
    try:
        out = subprocess.run((sys.executable, str(gen), "--check"),
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"changelog: could not run {gen.name} ({exc}) — page not verified.")
        return
    if out.returncode == 0:
        return
    bar = "!" * 72
    print(bar)
    print("  THE PUBLISHED PAGE WILL NOT BUILD from this changelog:")
    for line in (out.stdout + out.stderr).strip().splitlines():
        print(f"    {line}")
    print("")
    print("  The in-app What's-new reads the changelog live, so it looks fine;")
    print("  the generated page is the one that silently stops updating.")
    print(bar)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 when the changelog is behind (default: warn "
                         "and exit 0)")
    ap.add_argument("--ref", default=None,
                    help="git ref to compare against (default: main, or HEAD "
                         "when there is no main)")
    ap.add_argument("--limit", type=int, default=15,
                    help="how many missing commits to list (default: 15)")
    args = ap.parse_args()

    path = _source_path()
    newest = newest_entry_date(path)
    if newest is None:
        print(f"changelog: cannot read any entry from {path.name} "
              f"— skipping the freshness check.")
        return 1 if args.strict else 0

    ref = args.ref
    if ref is None:
        ok = subprocess.run(("git", "-C", str(_ROOT), "rev-parse", "--verify",
                             "--quiet", "main"), capture_output=True)
        ref = "main" if ok.returncode == 0 else "HEAD"

    try:
        missing = commits_since(ref, newest)
    except subprocess.CalledProcessError as exc:
        print(f"changelog: git log failed ({exc}) — skipping the check.")
        return 1 if args.strict else 0

    # Before the freshness verdict, because "up to date" is exactly what made
    # the broken page invisible: the changelog WAS current, only the generated
    # page had stopped building.
    report_publishability(path)

    if not missing:
        print(f"changelog: up to date (newest entry {newest}, "
              f"no user-facing commits after it on {ref}).")
        return 0

    bar = "!" * 72
    print(bar)
    print(f"  CHANGELOG IS BEHIND: newest entry is {newest}, but {len(missing)} "
          f"user-facing")
    print(f"  commit(s) landed on {ref} after that date:")
    print("")
    for sha, cdate, subject in missing[:args.limit]:
        print(f"    {cdate}  {sha}  {subject[:60]}")
    if len(missing) > args.limit:
        print(f"    ... and {len(missing) - args.limit} more")
    print("")
    print("  A user-facing change needs one entry in the curated changelog")
    print(f"  ({path}) — one entry per language you publish,")
    print("  dated the day it shipped. Every What's-new surface reads that file.")
    print("  Nothing here is user-facing? Then this run is noise: carry on.")
    print(bar)
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
