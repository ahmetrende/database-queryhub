#!/usr/bin/env python3
"""Check What's-new entries against the writing rules they are held to.

Entries are written for someone scanning a page, with the rules of the
`i-have-adhd` skill (.claude/skills/i-have-adhd/SKILL.md), the limits in
CLAUDE.md's changelog section, and, for the English side, the hard rules of
the `asd-ste100` skill (.claude/skills/asd-ste100/scripts/ste-lint.py: no
semicolons, no phrasal verbs, no marketing adjectives, no nominalization, no
long sentences, one word per action). The STE check is skipped when the skill
is not in the tree, as in the exported open-source repo.

  * the summary leads with what the reader gets: at most 25 words;
  * each bullet is one idea: at most 20 words; at most five bullets;
  * the title is short: at most 10 words;
  * no preamble, no closing pleasantries, no hedging adverbs, no idioms;
  * English and Turkish say the same thing: the same number of bullets, of
    the same kinds, in the same order.

Prints every entry that breaks a rule. Like check_changelog_fresh.py it EXITS
0 by default, because a wordy note is not a reason to refuse a push;
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

MAX_TITLE_WORDS = 10
MAX_SUMMARY_WORDS = 25
MAX_BULLET_WORDS = 20
MAX_BULLETS = 5

# Phrases the skill forbids. Whole words, case-insensitive. The English list
# lives here; the Turkish one lives in the changelog file itself, under
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


def check_entry(e: dict, forbidden: dict | None = None) -> list[str]:
    """Every rule this entry breaks, as one line each."""
    problems = []
    kinds = {}
    for lang in ("en", "tr"):
        side = e.get(lang) or {}
        t, s, p = side.get("t", ""), side.get("s", ""), side.get("p") or []
        if _words(t) > MAX_TITLE_WORDS:
            problems.append(f"{lang} title has {_words(t)} words (max {MAX_TITLE_WORDS})")
        if not s.strip():
            problems.append(f"{lang} summary is empty")
        elif _words(s) > MAX_SUMMARY_WORDS:
            problems.append(f"{lang} summary has {_words(s)} words (max {MAX_SUMMARY_WORDS})")
        if len(p) > MAX_BULLETS:
            problems.append(f"{lang} has {len(p)} bullets (max {MAX_BULLETS})")
        for i, b in enumerate(p, 1):
            n = _words(b.get("t", ""))
            if n > MAX_BULLET_WORDS:
                problems.append(f"{lang} bullet {i} has {n} words (max {MAX_BULLET_WORDS})")
        for where, text in [("title", t), ("summary", s)] + [(f"bullet {i}", b.get("t", ""))
                                                              for i, b in enumerate(p, 1)]:
            for phrase in _hits(text, lang, forbidden):
                problems.append(f"{lang} {where} uses \"{phrase}\"")
            if lang == "en":
                for hit in _ste_hits(text):
                    problems.append(f"en {where} breaks STE {hit}")
        kinds[lang] = [b.get("k") for b in p]
    if kinds.get("en") != kinds.get("tr"):
        problems.append(f"en and tr bullets differ: {kinds.get('en')} vs {kinds.get('tr')}")
    return problems


def check(entries: list[dict], forbidden: dict | None = None) -> dict[str, list[str]]:
    out = {}
    for e in entries:
        problems = check_entry(e, forbidden)
        if problems:
            out[e.get("id", "?")] = problems
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
    if isinstance(data, dict):
        for lang, words in ((data.get("style") or {}).get("forbidden") or {}).items():
            forbidden[lang] = list(forbidden.get(lang, [])) + list(words)
    found = check(entries, forbidden)
    for eid, problems in found.items():
        print(f"{eid}:")
        for p in problems:
            print(f"  - {p}")
    print(f"changelog style: {len(entries)} entries, {len(found)} with problems")
    return 1 if (found and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
