#!/usr/bin/env python3
"""Bring work that was written IN a downstream replica back up into this repo.

`sync_downstream_replica.py` goes one way: it REBUILDS the replica's tree from
this repository. That is safe for as long as the replica is only ever read —
and it stops being safe the moment somebody develops there, which is exactly
what the IdP integration does (PLA-479: their pull request merges into the
replica, not here).

A rebuild does not merge. It writes a tree, and a tree that is missing their
files is a perfectly valid tree, so the next downstream sync would delete their
work with no error and no conflict. This script is the other direction, and the
sync script now refuses to run while there is anything here left to take.

    export QH_REPLICA_REMOTE=https://…/replica.git
    python3 scripts/import_from_replica.py --since <replica-sha>
    # …then review the branch, run the gates, and open a pull request.

What it does NOT do: push, merge, or invent a commit for the replica. It leaves
a branch in this repository and prints what it found.

The baseline
------------
"What is theirs" means "everything on the replica since the last tree WE put
there". Every sync commit now carries an `Upstream-Commit:` trailer naming the
commit it was built from, and this script takes the newest one as the baseline.
The trailer arrived after the 1.0.21 sync, so for the first import the baseline
is passed with `--since`.

Authorship
----------
Their commits are replayed as ONE commit here — the replica squash-merges, and
a merge commit imported into a linear history is noise — but the message names
every commit it carries and every author is kept as a `Co-Authored-By` trailer.
An import that silently reattributes someone's work is worse than no tool.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Paths that belong to the replica and must never travel upstream. One list,
# declared by the sync script, so the two directions cannot disagree about
# which files are the replica's own.
from sync_downstream_replica import LOCAL_ONLY  # noqa: E402

TRAILER = "Upstream-Commit:"


def git(*args: str, check: bool = True) -> str:
    out = subprocess.run(("git", *args), cwd=str(ROOT),
                         capture_output=True, text=True)
    if check and out.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{out.stderr.strip()}")
    return out.stdout


def newest_marker(ref: str) -> str | None:
    """The replica commit that last carried one of our syncs, by its trailer.

    Read from the replica's own history rather than remembered here: a note in
    this repository would be one more thing to keep in step, and the fact
    belongs to the commit that states it."""
    log = git("log", "--format=%H%x00%B%x1e", ref)
    for entry in log.split("\x1e"):
        if not entry.strip():
            continue
        sha, _, body = entry.partition("\x00")
        for line in body.splitlines():
            if line.strip().startswith(TRAILER):
                return sha.strip()
    return None


def theirs(baseline: str, ref: str) -> list[dict]:
    """Commits on the replica after the baseline — theirs by definition."""
    out = []
    log = git("log", "--reverse", "--no-merges",
              "--format=%H%x00%an%x00%ae%x00%s", f"{baseline}..{ref}")
    for line in log.splitlines():
        if not line.strip():
            continue
        sha, an, ae, subject = line.split("\x00", 3)
        out.append({"sha": sha, "name": an, "email": ae, "subject": subject})
    return out


def changed(baseline: str, ref: str) -> list[str]:
    """Paths their commits touched, minus the replica-local ones.

    The exclusion is the whole reason this cannot be a `git cherry-pick`:
    CODEOWNERS names reviewers of the downstream org, and importing it would
    push that file into every other consumer of this repository, including the
    public one."""
    local = {p for p, _ in LOCAL_ONLY}
    return [p for p in git("diff", "--name-only", baseline, ref).splitlines()
            if p.strip() and p.strip() not in local]


def coauthors(commits: list[dict]) -> list[str]:
    seen, out = set(), []
    for c in commits:
        line = f"Co-Authored-By: {c['name']} <{c['email']}>"
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def message(commits: list[dict], baseline: str, ref: str) -> str:
    head = ("import the replica's own commits\n\n"
            "Written downstream and merged there, so they exist nowhere else. "
            "A downstream sync rebuilds that tree from this one and would "
            "delete them without an error, which is why they come up first.\n\n"
            f"Replica {baseline[:8]}..{ref[:8]}, {len(commits)} commit(s):\n")
    body = "".join(f"  {c['sha'][:8]}  {c['subject']}\n" for c in commits)
    return head + body + "\n" + "\n".join(coauthors(commits)) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--remote", default=os.environ.get("QH_REPLICA_REMOTE"),
                    help="replica git URL (or set QH_REPLICA_REMOTE)")
    ap.add_argument("--replica-branch", default="main")
    ap.add_argument("--since", help="baseline replica commit; default is the "
                                    "newest one carrying an Upstream-Commit trailer")
    ap.add_argument("--branch", default="chore/import-from-replica",
                    help="branch to create here (must exist nowhere yet)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be imported and stop")
    args = ap.parse_args(argv)

    if not args.remote:
        sys.stderr.write("no replica remote: pass --remote or set QH_REPLICA_REMOTE\n")
        return 2

    print(f"fetching {args.replica_branch} from the replica…")
    git("fetch", args.remote, args.replica_branch)
    ref = git("rev-parse", "FETCH_HEAD").strip()

    baseline = args.since or newest_marker(ref)
    if not baseline:
        sys.stderr.write(
            "no baseline: the replica carries no Upstream-Commit trailer yet, so "
            "there is nothing to measure 'since' against. Pass --since <sha> "
            "naming the last commit this repository put there.\n")
        return 2
    baseline = git("rev-parse", baseline).strip()

    commits = theirs(baseline, ref)
    paths = changed(baseline, ref)
    if not commits and not paths:
        print(f"  nothing to import — the replica is at our own {baseline[:8]}")
        return 0

    print(f"  baseline {baseline[:8]} → replica {ref[:8]}")
    for c in commits:
        print(f"    {c['sha'][:8]}  {c['name']:<22} {c['subject'][:60]}")
    print(f"  {len(paths)} file(s) to import"
          + (f", {len(LOCAL_ONLY)} replica-local path(s) excluded" if LOCAL_ONLY else ""))
    if args.dry_run:
        print("\ndry run — nothing written.")
        return 0

    if git("rev-parse", "--verify", "--quiet", args.branch, check=False).strip():
        sys.exit(f"branch {args.branch} already exists here — delete it or pass "
                 f"--branch with another name.")

    start = git("rev-parse", "HEAD").strip()
    git("checkout", "-q", "-b", args.branch)
    # Checkout the paths from the replica rather than applying a patch: their
    # tree is the authority for those files, and a patch would conflict against
    # anything that moved upstream in the meantime — noise, since the whole
    # point is to take THEIR version of the files they touched.
    git("checkout", ref, "--", *paths)
    git("add", "--", *paths)
    msg = message(commits, baseline, ref)
    proc = subprocess.run(("git", "commit", "-F", "-"), cwd=str(ROOT),
                          input=msg, capture_output=True, text=True)
    if proc.returncode != 0:
        git("checkout", "-q", start)
        sys.exit(f"commit failed:\n{proc.stderr.strip()}")

    print(f"\n  branch {args.branch} created on {start[:8]}")
    print("  now: run the gates, review the diff, and open a pull request.")
    print("    python3 scripts/check_repo_clean.py && python3 -m pytest -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
