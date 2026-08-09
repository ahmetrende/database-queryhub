#!/usr/bin/env python3
"""Sync this repository's tree into a downstream replica, preserving the few
files that exist only there.

A replica is a mirror someone else reads: same code, different home, plus a
handful of files that belong to that home and must never come back upstream —
`.github/CODEOWNERS` names reviewers of the downstream organisation, so upstream
cannot carry it (it would follow into every other consumer, including public
ones).

The sync therefore cannot be a plain push. It builds ONE commit whose tree is
this repository's tree plus those local-only files, read back from the replica
itself. Doing that by hand is what this replaces: every sync so far restored
CODEOWNERS from memory, and a sync that forgot would have deleted the reviewers
the branch protection depends on — silently, because a deletion is a perfectly
valid tree.

So the file list is declared, restored, and then VERIFIED before anything is
pushed. A missing entry is a hard failure, not a warning.

    export QH_REPLICA_REMOTE=https://…/replica.git
    python3 scripts/sync_downstream_replica.py \\
        --branch chore/TICKET-1-sync --message-file /tmp/msg.txt

    …then open a pull request; this never pushes to the replica's default branch.

The remote is taken from the environment or `--remote` and has NO default: a
mirror's address is deployment configuration, not something a public repository
should state.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Files that live in the replica and nowhere else. Restored from the replica on
# every sync, and verified afterwards.
#
# Keep this list SHORT and keep the reason with each entry: anything in here is
# invisible to upstream review, so it is exactly the kind of file that rots.
LOCAL_ONLY: tuple[tuple[str, str], ...] = (
    (".github/CODEOWNERS",
     "names reviewers of the downstream org; upstream must stay org-neutral"),
)


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    out = subprocess.run(("git", *args), cwd=str(cwd or ROOT),
                         capture_output=True, text=True)
    if check and out.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{out.stderr.strip()}")
    return out.stdout


def blob_at(ref: str, path: str) -> str | None:
    """The blob id of `path` at `ref`, or None when it is absent."""
    out = subprocess.run(("git", "rev-parse", f"{ref}:{path}"),
                         cwd=str(ROOT), capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def build_tree(base: str, restore: dict[str, str]) -> str:
    """`base`'s tree with `restore` (path -> blob id) laid on top.

    A temporary index, so the caller's staged work is untouched — this runs
    mid-session on a working checkout, not in a clean room.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        subprocess.run(("git", "read-tree", base), cwd=str(ROOT), env=env,
                       check=True, capture_output=True)
        for path, blob in restore.items():
            subprocess.run(("git", "update-index", "--add", "--cacheinfo",
                            f"100644,{blob},{path}"),
                           cwd=str(ROOT), env=env, check=True,
                           capture_output=True)
        out = subprocess.run(("git", "write-tree"), cwd=str(ROOT), env=env,
                             check=True, capture_output=True, text=True)
        return out.stdout.strip()


def changed_paths(a: str, b: str) -> list[str]:
    return [p for p in git("diff", "--name-only", a, b).splitlines() if p]


def signature_present(commit_object: str) -> bool:
    """True when the raw commit object carries a signature HEADER.

    Header only, deliberately: a commit whose message happens to contain the word
    gpgsig would otherwise pass. Headers end at the first blank line.
    """
    return "gpgsig" in commit_object.split("\n\n", 1)[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--remote", default=os.environ.get("QH_REPLICA_REMOTE"),
                    help="replica git URL (or set QH_REPLICA_REMOTE)")
    ap.add_argument("--replica-branch", default="main",
                    help="branch the new commit is PARENTED on, and the branch the "
                         "local-only files are read from (default: main). Pass the "
                         "pull-request branch to ADD a commit to an open PR — with "
                         "the default, the commit parents on main instead and the "
                         "push is a non-fast-forward the ruleset will not allow.")
    ap.add_argument("--branch", required=True,
                    help="branch to create on the replica for the pull request")
    ap.add_argument("--base", default="HEAD",
                    help="local ref whose tree is synced (default: HEAD)")
    ap.add_argument("--message-file", help="commit message file")
    ap.add_argument("--push", action="store_true",
                    help="push the branch; otherwise print the command")
    ap.add_argument("--no-sign", action="store_true",
                    help="do not sign (the replica's ruleset wants a signature, "
                         "so this is for dry runs and tests only)")
    args = ap.parse_args(argv)

    if not args.remote:
        return int(bool(sys.stderr.write(
            "no replica remote: pass --remote or set QH_REPLICA_REMOTE\n"))) or 2

    base = git("rev-parse", args.base).strip()

    print(f"fetching {args.replica_branch} from the replica…")
    git("fetch", args.remote, args.replica_branch)
    head = git("rev-parse", "FETCH_HEAD").strip()

    # Read the local-only files back from the replica. Absent there means the
    # list is wrong or a previous sync already dropped the file; either way a
    # human has to look, so stop.
    restore: dict[str, str] = {}
    for path, why in LOCAL_ONLY:
        blob = blob_at("FETCH_HEAD", path)
        if blob is None:
            sys.exit(f"FAILED: {path} is not in the replica's "
                     f"{args.replica_branch} — it {why}. Either it was lost by "
                     f"an earlier sync (restore it there first) or this list is "
                     f"out of date.")
        restore[path] = blob
        print(f"  keeping {path} ({blob[:8]}) — {why}")

    tree = build_tree(base, restore)

    # Verify, do not trust. Two directions:
    #   1. every declared file is present and byte-identical to the replica's
    #   2. nothing ELSE differs from base — a sync must not invent changes
    for path, blob in restore.items():
        got = git("rev-parse", f"{tree}:{path}").strip()
        if got != blob:
            sys.exit(f"FAILED: {path} came out as {got[:8]}, expected "
                     f"{blob[:8]} — the restore did not take")
    unexpected = [p for p in changed_paths(base, tree) if p not in restore]
    if unexpected:
        sys.exit("FAILED: the synced tree differs from "
                 f"{args.base} beyond the local-only list: {unexpected}")
    print(f"  tree {tree[:8]} = {args.base} + {len(restore)} local-only file(s)")

    msg = (Path(args.message_file).read_text(encoding="utf-8")
           if args.message_file else f"sync the replica with {base[:7]}\n")
    # commit-tree reads the message from stdin with `-F -`, so this one call does
    # not go through git() above. `-S` is explicit because commit-tree ignores
    # commit.gpgsign — a config-only setup would produce an unsigned commit and
    # the ruleset would reject the pull request after the push.
    cmd = ["git", "commit-tree", "-p", head, "-F", "-", tree]
    if not args.no_sign:
        cmd.insert(2, "-S")
    proc = subprocess.run(cmd, cwd=str(ROOT), input=msg,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"commit-tree failed:\n{proc.stderr.strip()}")
    commit = proc.stdout.strip()

    if not args.no_sign and not signature_present(git("cat-file", "commit", commit)):
        sys.exit("FAILED: the commit is unsigned; the replica's ruleset requires "
                 "a verified signature on every commit in the pull request")
    state = "unsigned — dry run" if args.no_sign else "signed"
    print(f"  commit {commit} ({state}), parent {head[:8]}")

    ref = f"{commit}:refs/heads/{args.branch}"
    if args.push:
        git("push", args.remote, ref)
        print(f"pushed {args.branch}; open a pull request against "
              f"{args.replica_branch}")
    else:
        print("\nnothing pushed. To push:\n"
              f"  git push {args.remote} {ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
