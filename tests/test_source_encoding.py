"""Source files must not contain characters that are invisible or that
impersonate ASCII.

Both failure modes are real and both are silent. A NUL byte makes grep treat
the file as binary, so the file stops appearing in search results while still
compiling — you lose the ability to find your own code. A non-breaking space
looks exactly like a space in every editor and diff, but is not SQL or Python
whitespace, so it turns into a syntax error or a subtly different string that
nobody can see.

Deliberate typography is fine and stays allowed: curly quotes in user-facing
strings, the zero-width space that neutralises Slack code fences, the BOM in a
CSV parser test. Only the two characters that are never intentional here are
rejected.
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Extensions worth checking: anything a human edits and a parser reads.
TEXT_SUFFIXES = {
    ".py", ".jsx", ".js", ".mjs", ".css", ".html", ".md", ".sql", ".sh",
    ".toml", ".yml", ".yaml", ".txt", ".in", ".cfg", ".service", ".json",
}

# The characters are written as ESCAPES on purpose: a literal NBSP in this
# file would make it fail its own check as soon as the file became tracked,
# and the escape also tells the reader exactly which character is meant.
FORBIDDEN = {
    "\x00": "NUL byte (makes grep skip the file as binary)",
    "\u00a0": "non-breaking space (looks like a space, is not whitespace)",
}


def _tracked_text_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    for name in out.stdout.split("\0"):
        if not name:
            continue
        p = ROOT / name
        if p.suffix in TEXT_SUFFIXES and p.is_file():
            yield p


def test_there_are_files_to_check():
    """Guard against the glob silently matching nothing."""
    assert len(list(_tracked_text_files())) > 100


@pytest.mark.parametrize("char,why", list(FORBIDDEN.items()),
                         ids=["nul", "nbsp"])
def test_no_forbidden_invisible_characters(char, why):
    hits = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if char in text:
            line = next((i for i, ln in enumerate(text.splitlines(), 1)
                         if char in ln), 0)
            hits.append(f"{path.relative_to(ROOT)}:{line}")
    assert hits == [], f"{why}: " + ", ".join(hits)
