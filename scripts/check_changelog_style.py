#!/usr/bin/env python3
"""Check What's-new entries against the writing rules they are held to.

Entries are written for someone who scans a page. The rules come from the
`i-have-adhd` skill (.claude/skills/i-have-adhd/SKILL.md), the limits in
CLAUDE.md's changelog section, and, for the English side, the hard rules of
the `asd-ste100` skill (.claude/skills/asd-ste100/scripts/ste-lint.py). The
STE check is skipped when the skill is not in the tree, as in the exported
open-source repo.

Version 2 entry shape (see src/queryhub/web/changelog.py):

  * the audience `a` is `user` or `admin`;
  * the title has at most 8 words, the tl;dr summary at most 20;
  * a bullet is a result line `h` (at most 9 words) plus an optional detail
    `d` (at most 20 words); at most five bullets; the old `t` bullet with a
    bold lead is refused;
  * plain text only: no double asterisks, no backticks, no em or en dash;
  * a breaking entry (`brk`) carries an action line `act` (at most 14 words)
    in every language, and only a breaking entry carries one;
  * a screenshot (`img.before`, `img.after`) is a file in docs/whatsnew and has
    alt text in every language;
  * no preamble, no closing pleasantries, no hedging adverbs, no idioms;
  * English and Turkish say the same thing: the same number of bullets, of
    the same kinds, in the same order.

Prints every entry that breaks a rule. Like check_changelog_fresh.py it EXITS
0 by default, because a wordy note is not a reason to refuse a push.
`--strict` exits 1 for a caller that wants it to fail (a publish step, a test).

    python scripts/check_changelog_style.py                 # warn
    python scripts/check_changelog_style.py --strict        # exit 1 on any hit
    python scripts/check_changelog_style.py --file x.json   # another file
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

LANGS = ("en", "tr")
AUDIENCES = ("user", "admin")
KINDS = ("new", "improved", "fixed", "changed")
MAX_TITLE_WORDS = 8
MAX_SUMMARY_WORDS = 20
MAX_HEAD_WORDS = 9
MAX_DETAIL_WORDS = 20
MAX_ACTION_WORDS = 14
MAX_BULLETS = 5

# Screenshots live here. Mock data only.
MEDIA_DIR = _ROOT / "docs" / "whatsnew"
_MEDIA_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}\.(png|webp|gif|jpg)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Markup that a page would show literally.
_MARKUP = [("**", "\"**\""), ("`", "a backtick"), ("—", "an em dash"),
           ("–", "an en dash")]

# Phrases the skill forbids. Whole words, case-insensitive. The English list
# lives here. The Turkish one lives in the changelog file itself, under
# "style": {"forbidden": {"tr": [...]}}, because this repo is English-only and
# the changelog is the bilingual file that sits outside it.
_FORBIDDEN = {
    "en": [
        "great question", "let me", "i'll", "sure!", "looking at your", "to answer",
        "let us know", "hope this helps", "happy to", "feel free",
        "perhaps", "might", "could possibly", "possibly",
        "circle back", "get the ball rolling", "on the same page", "under the hood",
        "out of the box", "seamless", "seamlessly", "we're excited", "we are excited",
        "by the way",
    ],
}


def _source_path() -> Path:
    """The curated changelog, found the way the app finds it (see
    check_changelog_fresh.py, which this mirrors)."""
    try:
        sys.path.insert(0, str(_ROOT / "src"))
        from queryhub.web import changelog  # noqa: PLC0415
        return changelog._source_path()
    except Exception:
        return _ROOT.parent / "site" / "changelog.json"


_STE_LINT = _ROOT / ".claude" / "skills" / "asd-ste100" / "scripts" / "ste-lint.py"
_ste = None


def _ste_lint():
    """The vendored STE linter's lint(), or None when the skill is absent."""
    global _ste
    if _ste is None:
        _ste = False
        if _STE_LINT.exists():
            import importlib.util  # noqa: PLC0415
            spec = importlib.util.spec_from_file_location("ste_lint", _STE_LINT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _ste = mod.lint
    return _ste or None


def _ste_hits(text: str) -> list[str]:
    """Hard STE findings in one English text, as "rule: message"."""
    lint = _ste_lint()
    if not lint or not (text or "").strip():
        return []
    findings, _words_total = lint(re.sub(r"[*`]", "", text))
    return [f"{f['rule']}: {f['message']}" for f in findings
            if f.get("level") == "advisory-free"]


def _words(text: str) -> int:
    """Words a reader sees: markdown emphasis and code ticks removed."""
    plain = re.sub(r"[*`]", "", text or "")
    return len([w for w in plain.split() if re.search(r"\w", w)])


def _hits(text: str, lang: str, forbidden: dict | None = None) -> list[str]:
    low = (text or "").lower()
    out = []
    for phrase in (forbidden or _FORBIDDEN).get(lang, []):
        if re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", low):
            out.append(phrase)
    return out


def _limit(problems: list[str], where: str, text: str, limit: int) -> None:
    n = _words(text)
    if n > limit:
        problems.append(f"{where} has {n} words (max {limit})")


def _texts(side: dict) -> list[tuple[str, str]]:
    """Every reader-facing text of one language side, with its place."""
    out = [("title", side.get("t", "")), ("summary", side.get("s", ""))]
    if side.get("act"):
        out.append(("action", side["act"]))
    for which, alt in (side.get("alt") or {}).items():
        out.append((f"{which} alt text", alt or ""))
    for i, b in enumerate(side.get("p") or [], 1):
        if isinstance(b, dict):
            out.append((f"bullet {i} result line", b.get("h", "")))
            if b.get("d"):
                out.append((f"bullet {i} detail", b["d"]))
    return out


def _check_images(e: dict, problems: list[str], media_dir: Path) -> list[str]:
    """Problems with `img`, and the image slots that need alt text."""
    img = e.get("img")
    if img is None:
        return []
    if not isinstance(img, dict) or not (set(img) & {"before", "after"}):
        problems.append("img must be {\"before\": file, \"after\": file} with at least one file")
        return []
    extra = set(img) - {"before", "after"}
    if extra:
        problems.append(f"img has unknown keys: {sorted(extra)}")
    if "before" in img and "after" not in img:
        problems.append("img has a before image with no after image")
    slots = []
    for which in ("before", "after"):
        name = img.get(which)
        if name is None:
            continue
        slots.append(which)
        if not isinstance(name, str) or not _MEDIA_NAME.match(name):
            problems.append(f"img.{which} \"{name}\" is not a plain lower-case image file name")
        elif not (media_dir / name).is_file():
            problems.append(f"img.{which} \"{name}\" is not in docs/whatsnew")
    return slots


def check_entry(e: dict, forbidden: dict | None = None,
                media_dir: Path | None = None) -> list[str]:
    """Every rule this entry breaks, as one line each."""
    problems: list[str] = []
    if e.get("a") not in AUDIENCES:
        problems.append(f"audience must be user or admin, not {e.get('a')!r}")
    if not _DATE.match(str(e.get("d", ""))):
        problems.append(f"date must be YYYY-MM-DD, not {e.get('d')!r}")
    breaking = bool(e.get("brk"))
    slots = _check_images(e, problems, media_dir or MEDIA_DIR)
    kinds = {}
    for lang in LANGS:
        side = e.get(lang) or {}
        t, s, p = side.get("t", ""), side.get("s", ""), side.get("p") or []
        if not t.strip():
            problems.append(f"{lang} title is empty")
        _limit(problems, f"{lang} title", t, MAX_TITLE_WORDS)
        if not s.strip():
            problems.append(f"{lang} summary is empty")
        _limit(problems, f"{lang} summary", s, MAX_SUMMARY_WORDS)
        act = (side.get("act") or "").strip()
        if breaking and not act:
            problems.append(f"{lang} has brk but no action line (act)")
        if act and not breaking:
            problems.append(f"{lang} has an action line (act) but the entry has no brk")
        _limit(problems, f"{lang} action", act, MAX_ACTION_WORDS)
        alt = side.get("alt") or {}
        for which in slots:
            if not (alt.get(which) or "").strip():
                problems.append(f"{lang} has no alt text for the {which} image")
        if len(p) > MAX_BULLETS:
            problems.append(f"{lang} has {len(p)} bullets (max {MAX_BULLETS})")
        for i, b in enumerate(p, 1):
            if not isinstance(b, dict):
                problems.append(f"{lang} bullet {i} is not an object")
                continue
            if "t" in b and "h" not in b:
                problems.append(f"{lang} bullet {i} uses the old shape (t): write h and d")
                continue
            if b.get("k") not in KINDS:
                problems.append(f"{lang} bullet {i} kind must be one of {', '.join(KINDS)}")
            if not (b.get("h") or "").strip():
                problems.append(f"{lang} bullet {i} has no result line (h)")
            _limit(problems, f"{lang} bullet {i} result line", b.get("h", ""), MAX_HEAD_WORDS)
            _limit(problems, f"{lang} bullet {i} detail", b.get("d", ""), MAX_DETAIL_WORDS)
        for where, text in _texts(side):
            for mark, name in _MARKUP:
                if mark in (text or ""):
                    problems.append(f"{lang} {where} contains {name}")
            for phrase in _hits(text, lang, forbidden):
                problems.append(f"{lang} {where} uses \"{phrase}\"")
            if lang == "en":
                for hit in _ste_hits(text):
                    problems.append(f"en {where} breaks STE {hit}")
        kinds[lang] = [b.get("k") for b in p if isinstance(b, dict)]
    if kinds.get("en") != kinds.get("tr"):
        problems.append(f"en and tr bullets differ: {kinds.get('en')} vs {kinds.get('tr')}")
    return problems


def check(entries: list[dict], forbidden: dict | None = None,
          media_dir: Path | None = None, categories: list[str] | None = None,
          ) -> dict[str, list[str]]:
    """Problems per entry id. Also flags a repeated id and an unknown category."""
    out: dict[str, list[str]] = {}
    seen: set[str] = set()
    for e in entries:
        eid = e.get("id") or "?"
        problems = check_entry(e, forbidden, media_dir)
        if eid in seen:
            problems.insert(0, "the id is not unique")
        seen.add(eid)
        if categories and e.get("c") not in categories:
            problems.append(f"category {e.get('c')!r} is not in the file's categories")
        if problems:
            out.setdefault(eid, []).extend(problems)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--strict", action="store_true", help="exit 1 when any entry breaks a rule")
    ap.add_argument("--file", type=Path, default=None, help="check this file instead")
    args = ap.parse_args(argv)
    path = args.file or _source_path()
    if not path.exists():
        print(f"changelog style: {path} not found, nothing to check")
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data["entries"] if isinstance(data, dict) else data
    forbidden = dict(_FORBIDDEN)
    categories = None
    if isinstance(data, dict):
        for lang, words in ((data.get("style") or {}).get("forbidden") or {}).items():
            forbidden[lang] = list(forbidden.get(lang, [])) + list(words)
        cats = data.get("categories") or []
        categories = [c if isinstance(c, str) else c.get("key") for c in cats] or None
    found = check(entries, forbidden, categories=categories)
    for eid, problems in found.items():
        print(f"{eid}:")
        for p in problems:
            print(f"  - {p}")
    print(f"changelog style: {len(entries)} entries, {len(found)} with problems")
    return 1 if (found and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
