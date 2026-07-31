#!/usr/bin/env python3
"""Print the CHANGELOG section for one version, for release notes.

A script rather than inline YAML for the same reason as
scripts/ci_demo_roundtrip.py: a heredoc inside a `run: |` block cannot
terminate, because the block scalar indents the closing token. It is also
testable, which an embedded snippet is not.

    python scripts/release_notes.py v0.2.0
"""
from __future__ import annotations

import pathlib
import re
import sys


def extract(changelog: str, version: str) -> str | None:
    """The body of the section for `version`, or None if there isn't one.

    Handles both `## [0.2.0] - 2026-07-25` (keep-a-changelog) and a bare
    `## 0.2.0`, and stops at the next `## ` heading or end of file.
    """
    v = version.lstrip("v")
    pattern = rf"^## \[?{re.escape(v)}\]?.*?$(.*?)(?=^## |\Z)"
    m = re.search(pattern, changelog, re.M | re.S)
    return m.group(1).strip() if m else None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    version = argv[1]
    path = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    body = extract(path.read_text(encoding="utf-8"), version)
    if body:
        print(body)
        return 0
    # Not a failure: a release can predate its changelog entry, and an empty
    # release note is better than a failed release. Say so in the notes.
    print(f"No CHANGELOG section for {version}. See CHANGELOG.md for the "
          f"full history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
