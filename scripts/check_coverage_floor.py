#!/usr/bin/env python3
"""Per-file coverage floor for the modules that decide who may run what.

Why this exists instead of `coverage report --fail-under=80 --include=...`:
that form measures the AGGREGATE over the included files, so one thin module
hides behind the thick ones. It did. With the five original modules the gate
reported 88% and passed, while grants.py — half of the access-control system,
since it is the module that hands access out and takes it away — sat at 59%
with revoke() and both grantee notifications entirely unexecuted. The number
that was supposed to protect the access decisions was being satisfied by
query_safety.py's 642 well-covered statements.

So: every listed file must clear the floor ON ITS OWN. The aggregate is still
printed, because it is useful information, but it is not the gate.

Run after a coverage run:
    python -m pytest --cov=src/dba_slack_bot
    python scripts/check_coverage_floor.py
"""
from __future__ import annotations

import json
import subprocess
import sys

FLOOR = 80

# The modules that decide who may run what. A change that drops coverage here is
# a change to an access decision that nothing checks.
#
# Keep this list short and keep it real: adding a module you have no intention
# of covering turns the gate into noise, and removing one to make CI green is
# the failure mode this file exists to prevent.
GATED = [
    "query_safety.py",      # the safety classifier (leading keyword, tautology)
    "ast_safety.py",        # the sqlglot second pass
    "core_decide.py",       # the shared approve/reject state machine
    "grants.py",            # granting and revoking access
    "teams.py",             # effective_grant_for_user — resolution per submit
    "web/sessions.py",      # session mint / verify / revoke
]


def _coverage_json() -> dict:
    """`coverage json` to stdout. Fails loudly if there is no data — a missing
    .coverage file must not read as "nothing to check, all good"."""
    proc = subprocess.run([sys.executable, "-m", "coverage", "json", "-o", "-"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"could not read coverage data: {proc.stderr.strip()}\n"
                 f"Run the suite with --cov first.")
    return json.loads(proc.stdout)


def main() -> int:
    data = _coverage_json()
    files = data.get("files", {})
    if not files:
        sys.exit("coverage data is empty — did the suite run with --cov?")

    rows, missing, failures = [], [], []
    for suffix in GATED:
        matches = [p for p in files if p.replace("\\", "/").endswith("/" + suffix)
                   or p.replace("\\", "/") == suffix]
        if not matches:
            # A renamed or deleted module must break the build rather than
            # silently drop out of the gate.
            missing.append(suffix)
            continue
        for path in matches:
            pct = files[path]["summary"]["percent_covered"]
            rows.append((path, pct, files[path]["summary"]["missing_lines"]))
            if pct < FLOOR:
                failures.append((path, pct))

    width = max((len(p) for p, _, _ in rows), default=10)
    print(f"Per-file coverage floor: {FLOOR}%\n")
    for path, pct, miss in sorted(rows):
        mark = "ok  " if pct >= FLOOR else "FAIL"
        print(f"  {mark} {path:<{width}}  {pct:5.1f}%  ({miss} lines uncovered)")

    if rows:
        total_stmts = sum(files[p]["summary"]["num_statements"] for p, _, _ in rows)
        total_miss = sum(m for _, _, m in rows)
        agg = 100.0 * (total_stmts - total_miss) / total_stmts if total_stmts else 0
        print(f"\n  aggregate over the gated set: {agg:.1f}% "
              f"(informational — NOT the gate)")

    if missing:
        print("\nGated module not found in the coverage data:")
        for suffix in missing:
            print(f"  - {suffix}")
        print("If it moved, update GATED in this script. If it is gone, say so "
              "in the commit message — this list is the security surface.")

    if failures:
        print(f"\n{len(failures)} gated module(s) below {FLOOR}%:")
        for path, pct in failures:
            print(f"  - {path}: {pct:.1f}%")
        print("\nThese modules decide who may run what. Write the tests, or "
              "argue in the PR for why this one is different.")

    if missing or failures:
        return 1
    print("\nall gated modules clear the floor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
