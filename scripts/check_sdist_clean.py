#!/usr/bin/env python3
"""Build the source distribution and check what is actually inside it.

Every previous check on this question looked at the repository. The package is
not the repository: `graft QueryHubWeb` walks the filesystem, so it picks up
gitignored directories and follows symlinks, and a `prune` that names one path
says nothing about the same content sitting at another. Licensed fonts and
build output reached a package that way, past a prune that named one of the
three paths they lived at.

So this checks the artifact. Run it before publishing:

    python scripts/check_sdist_clean.py

Exits non-zero and prints every offending path. Also verifies the opposite
direction — that files the package genuinely needs are present — because an
over-broad exclude is just as shippable a mistake as a missing one.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Must NOT be in the package. Patterns, not paths: the point is to catch the
# content wherever it moved to.
FORBIDDEN = [
    (r"(^|/)_ds(/|$)", "design-system bundle (gitignored, so invisible to git checks)"),
    (r"design-canvas", "design-tool scaffolding"),
    (r"Logo\.html", "logo generator page"),
    (r"(^|/)screenshots(/|$)", "screenshots"),
    (r"(^|/)uploads(/|$)", "uploads"),
    (r"app/dist/", "frontend build output (stale by definition; the install builds it)"),
    (r"node_modules", "npm tree"),
    (r"\.env$", "environment file"),
    (r"master\.key", "encryption key"),
    (r"secrets\.enc", "encrypted secrets"),
    (r"\.coverage", "coverage data"),
    (r"metrics_dashboard\.html", "generated dashboard"),
]

# Must BE in the package, or the sdist cannot build or run.
REQUIRED = [
    "pyproject.toml",
    "LICENSE",
    "NOTICE",
    "README.md",
    "src/",
    "migrations/",
    "QueryHubWeb/QueryHub.html",          # design CSS source for gen-css.mjs
    "QueryHubWeb/app/package.json",       # so `npm ci && npm run build` works
    "QueryHubWeb/app/src/main.jsx",
    "QueryHubWeb/fonts/",                 # self-hosted OFL fonts
    "QueryHubWeb/fonts/LICENSE-fonts.txt",  # OFL requires the notice to travel
    "deploy/",
]


def build_sdist(outdir: Path) -> Path:
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(outdir)],
        cwd=ROOT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    tarballs = sorted(outdir.glob("*.tar.gz"))
    if not tarballs:
        raise SystemExit("build produced no sdist")
    return tarballs[-1]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tarball = build_sdist(Path(tmp))
        with tarfile.open(tarball) as tf:
            names = tf.getnames()

        # Strip the leading "queryhub-0.1.0/" so patterns read naturally.
        root_prefix = names[0].split("/")[0] + "/" if names else ""
        rel = [n[len(root_prefix):] for n in names if n.startswith(root_prefix)]

        print(f"sdist: {tarball.name}  ({len(rel)} entries)")

        problems = []
        for pattern, why in FORBIDDEN:
            hits = [n for n in rel if re.search(pattern, n)]
            if hits:
                problems.append((why, pattern, hits))

        missing = []
        for needed in REQUIRED:
            if not any(n == needed or n.startswith(needed) for n in rel):
                missing.append(needed)

        if problems:
            print("\nFORBIDDEN CONTENT IN THE PACKAGE:", file=sys.stderr)
            for why, pattern, hits in problems:
                print(f"\n  {why}  (/{pattern}/) — {len(hits)} path(s):",
                      file=sys.stderr)
                for h in hits[:8]:
                    print(f"      {h}", file=sys.stderr)
                if len(hits) > 8:
                    print(f"      ... and {len(hits) - 8} more", file=sys.stderr)
        if missing:
            print("\nMISSING FROM THE PACKAGE:", file=sys.stderr)
            for m in missing:
                print(f"  {m}", file=sys.stderr)

        if problems or missing:
            print("\nFix MANIFEST.in and run this again.", file=sys.stderr)
            return 1

        print("sdist clean — nothing forbidden, nothing missing.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
