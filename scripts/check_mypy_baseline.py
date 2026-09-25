#!/usr/bin/env python3
"""Fail on NEW mypy errors, measured against a committed baseline.

`mypy src` has never been clean -- the codebase is not fully typed -- so an
all-or-nothing gate could not be switched on. It ran advisory instead, and 242
errors in 29 files built up without failing a single build. This gate records
the errors that exist today in mypy_baseline.txt, next to this script, and
fails only when a change adds one.

An error is keyed by file, error code and message, WITHOUT its line number,
and keys are compared by count. Keyed on lines, every edit above an old error
would report it as new. The price: a new error worded exactly like an old one
in the same file hides behind it until the old one is fixed.

    python scripts/check_mypy_baseline.py            # the gate CI runs
    python scripts/check_mypy_baseline.py --update   # rewrite the baseline

Exit 0: nothing new, with a hint when errors were fixed so the baseline can be
tightened. Exit 1: new errors, listed with their line numbers. Exit 2: nothing
was compared, because the run cannot be trusted -- mypy crashed, stopped on a
blocking error such as a syntax error, wrote to stderr, or printed output that
does not add up -- or because the baseline file is malformed.

mypy's output depends on its version and on which third-party packages are
installed, so the version is pinned in pyproject.toml and the baseline is
generated from the CI install (`pip install -e ".[dev,slack]"`).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "mypy_baseline.txt"

# `mypy src` with the config in pyproject.toml, as CI always ran it. The flags
# only pin the output SHAPE this script parses, and none changes what mypy
# checks: without them a local `pretty = true` or an absolute-path setting
# would make every known error look new.
MYPY_ARGS = ["src", "--no-pretty", "--no-color-output", "--show-error-codes",
             "--hide-column-numbers", "--hide-error-end", "--hide-absolute-path",
             "--error-summary"]

# `path:line: error: message  [code]`, from mypy. The baseline stores the same
# line without `:line`, so one pattern reads both. Notes share the shape and
# are skipped: they elaborate on an error and are not errors themselves.
_ERROR_RE = re.compile(
    r"^(?P<path>.+?)(?::(?P<line>\d+)(?::\d+)?)?: (?P<severity>error|note): "
    r"(?P<message>.*?)(?:  \[(?P<code>[a-z0-9_-]+)\])?$")
_SUMMARY_RE = re.compile(
    r"^(?:Success: no issues found|Found (?P<count>\d+) errors? in \d+ files?)")
# A message can quote a line itself ("already defined on line 12"), and that
# number moves with unrelated edits exactly like the error's own.
_LINE_REF_RE = re.compile(r"\bline \d+\b")
_VERSION_RE = re.compile(r"^# generated-with: mypy (?P<version>\S+)$")

Key = tuple[str, str, str]          # (path, code, message)


def make_key(path: str, code: str | None, message: str) -> Key:
    return (path.replace("\\", "/"), code or "",
            _LINE_REF_RE.sub("line N", message.strip()))


def render(key: Key, line: int | None = None) -> str:
    """A key in mypy's own format; without `line`, as one baseline line."""
    path, code, message = key
    where = f"{path}:{line}" if line else path
    return f"{where}: error: {message}" + (f"  [{code}]" if code else "")


def parse_output(text: str) -> tuple[list[tuple[Key, int]], int | None]:
    """mypy's stdout -> ([(key, line number), ...], the error count its summary
    line states). The count is None when there is no summary line, which is
    what a crash or a truncated run looks like."""
    errors: list[tuple[Key, int]] = []
    stated: int | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _ERROR_RE.match(line)
        if m:
            if m["severity"] == "error":
                key = make_key(m["path"], m["code"], m["message"])
                errors.append((key, int(m["line"] or 0)))
            continue
        s = _SUMMARY_RE.match(line)
        if s:
            stated = int(s["count"] or 0)
    return errors, stated


def run_problem(returncode: int, stderr: str,
                errors: list[tuple[Key, int]], stated: int | None) -> str | None:
    """Why this run cannot be compared, or None when it can.

    Each check stands for a way the gate would otherwise pass without having
    looked: a crash has no errors to count, and a parser that silently missed
    lines would report fewer errors than there are."""
    if returncode not in (0, 1):
        return (f"exited {returncode} (a crash, or a blocking error such as a "
                f"syntax error)")
    if stated is None:
        return "printed no summary line"
    if stated != len(errors):
        return f"reported {stated} error(s) but {len(errors)} could be parsed"
    if (returncode == 0) != (not errors):
        return f"exited {returncode} but reported {len(errors)} error(s)"
    if stderr.strip():
        # A healthy run writes nothing here. A misspelled option in the
        # [tool.mypy] config does -- and mypy then ignores it, quietly
        # switching off a check this gate is supposed to hold.
        return "wrote to stderr"
    return None


def load_baseline(text: str) -> tuple[Counter[Key], str | None]:
    """Baseline text -> (count per key, the mypy version that wrote it)."""
    counts: Counter[Key] = Counter()
    version = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        v = _VERSION_RE.match(line)
        if v:
            version = v["version"]
            continue
        if not line or line.startswith("#"):
            continue
        m = _ERROR_RE.match(line)
        if not m or m["severity"] != "error":
            raise ValueError(
                f"{BASELINE.name} line {n} is not an error line: {line!r}")
        counts[make_key(m["path"], m["code"], m["message"])] += 1
    return counts, version


def format_baseline(keys: list[Key], version: str) -> str:
    header = [
        "# mypy errors that existed when this file was written. CI fails on any",
        "# error that is not listed here. One line per error, with the line number",
        "# removed so that unrelated edits do not move it. Regenerate rather than",
        "# hand-edit: python scripts/check_mypy_baseline.py --update",
        f"# generated-with: mypy {version}",
    ]
    return "\n".join(header + sorted(render(k) for k in keys)) + "\n"


def compare(errors: list[tuple[Key, int]], baseline: Counter[Key]
            ) -> tuple[dict[Key, list[int]], int, Counter[Key]]:
    """-> (keys with more occurrences than the baseline allows, mapped to every
    line they occur on now; how many occurrences are new; how many of each
    baseline key no longer occur)."""
    counts = Counter(k for k, _ in errors)
    over = {k for k, n in counts.items() if n > baseline[k]}
    new = {k: sorted(ln for kk, ln in errors if kk == k) for k in over}
    new_count = sum(counts[k] - baseline[k] for k in over)
    fixed = Counter({k: n - counts[k] for k, n in baseline.items()
                     if n > counts[k]})
    return new, new_count, fixed


def _mypy_version() -> str:
    try:
        return metadata.version("mypy")
    except metadata.PackageNotFoundError:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true",
                    help="rewrite the baseline from this run instead of checking")
    args = ap.parse_args(argv)

    proc = subprocess.run([sys.executable, "-m", "mypy", *MYPY_ARGS], cwd=ROOT,
                          capture_output=True, encoding="utf-8", errors="replace")
    errors, stated = parse_output(proc.stdout)
    problem = run_problem(proc.returncode, proc.stderr, errors, stated)
    if problem:
        tail = "\n".join(proc.stdout.splitlines()[-40:])
        print(f"mypy: {problem}; nothing was compared.\n", file=sys.stderr)
        if tail:
            print(f"--- stdout (last 40 lines) ---\n{tail}", file=sys.stderr)
        if proc.stderr.strip():
            print(f"--- stderr ---\n{proc.stderr.rstrip()}", file=sys.stderr)
        return 2

    running = _mypy_version()
    rel = BASELINE.relative_to(ROOT)
    if args.update:
        BASELINE.write_text(format_baseline([k for k, _ in errors], running),
                            encoding="utf-8")
        files = len({k[0] for k, _ in errors})
        print(f"mypy: wrote {len(errors)} errors in {files} files to {rel}")
        return 0

    try:
        baseline, recorded = load_baseline(BASELINE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"mypy: {rel} is missing; every error counts as new.")
        baseline, recorded = Counter(), None
    except ValueError as e:
        print(f"mypy: cannot read the baseline: {e}", file=sys.stderr)
        return 2

    new, new_count, fixed = compare(errors, baseline)
    print(f"mypy: {len(errors)} errors, baseline {sum(baseline.values())}, "
          f"{new_count} new")
    if recorded and recorded != running:
        print(f"note: the baseline was written by mypy {recorded} and this is "
              f"mypy {running}; another version words errors differently. "
              f"Install the pinned one: pip install -e '.[dev]'")

    if new:
        print(f"\nNew errors, not in {rel} (line numbers from this run):")
        for _, ln, key in sorted((k[0], ln, k) for k in new for ln in new[k]):
            # When the baseline allows some of these, the new occurrence cannot
            # be told apart from the old ones, so every one is listed.
            allowed = (f"  ({len(new[key])} now, {baseline[key]} allowed)"
                       if baseline[key] else "")
            print(f"  {render(key, ln)}{allowed}")
        print("\nFix them. An error accepted on purpose goes in with --update, "
              "and the commit says why.")
    if fixed:
        print(f"\n{sum(fixed.values())} baseline error(s) no longer occur. "
              f"Tighten the baseline so they cannot come back unnoticed:")
        print("  python scripts/check_mypy_baseline.py --update")
        for key in sorted(fixed):
            times = f"  (x{fixed[key]})" if fixed[key] > 1 else ""
            print(f"  - {render(key)}{times}")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
