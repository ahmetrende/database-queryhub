"""The replica sync must never silently drop a local-only file.

`.github/CODEOWNERS` exists in the replica and nowhere else, because it names
reviewers of that organisation and upstream has to stay org-neutral. Every sync
so far restored it from memory. A sync that forgot would have deleted the
reviewers the branch protection depends on, and nothing would have complained: a
tree with a file missing is a perfectly valid tree.

These tests build a real replica in a temp directory and run the real script
against it, because the failure mode is entirely about what git ends up holding.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync_downstream_replica.py"
_SPEC = importlib.util.spec_from_file_location("sync_downstream_replica", _SCRIPT)
sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync)

CODEOWNERS = ".github/CODEOWNERS"


def _git(cwd, *args, **kw):
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True,
                          text=True, check=kw.pop("check", True), **kw)


def _repo(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "commit.gpgsign", "false")
    return path


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """An upstream and a replica, the replica carrying one local-only file."""
    up = _repo(tmp_path / "up")
    (up / "app.py").write_text("print('v1')\n")
    _git(up, "add", "-A")
    _git(up, "commit", "-qm", "upstream v1")

    rep = _repo(tmp_path / "replica")
    (rep / "app.py").write_text("print('v0')\n")
    (rep / ".github").mkdir()
    (rep / CODEOWNERS).write_text("* @downstream/reviewers\n")
    _git(rep, "add", "-A")
    _git(rep, "commit", "-qm", "replica base")

    # Point the module at the temp upstream.
    monkeypatch.setattr(sync, "ROOT", up)
    return up, rep


def _run(rep, branch="chore/T-1-sync", extra=()):
    # --no-sign: the temp repo has no signing key. The signature guard has its
    # own test below, against the raw commit object.
    return sync.main(["--remote", str(rep), "--branch", branch, "--no-sign", *extra])


# ---------------------------------------------------------------------------
# the whole point
# ---------------------------------------------------------------------------

def test_the_local_only_file_survives_the_sync(world, capsys):
    up, rep = world
    assert _run(rep) == 0
    out = capsys.readouterr().out
    assert CODEOWNERS in out, "the sync did not report keeping the local-only file"
    # The commit it printed must actually contain the file, with the replica's
    # content — not upstream's (upstream has none).
    sha = [w for w in out.replace("\n", " ").split() if len(w) == 40][-1]
    blob = _git(up, "cat-file", "-p", f"{sha}:{CODEOWNERS}").stdout
    assert blob == "* @downstream/reviewers\n"


def test_the_synced_tree_is_upstreams_plus_that_file(world, capsys):
    up, rep = world
    assert _run(rep) == 0
    out = capsys.readouterr().out
    sha = [w for w in out.replace("\n", " ").split() if len(w) == 40][-1]
    # upstream's content won for a shared file...
    assert _git(up, "cat-file", "-p", f"{sha}:app.py").stdout == "print('v1')\n"
    # ...and the diff against upstream's tree is EXACTLY the local-only file.
    names = _git(up, "diff", "--name-only", "HEAD", sha).stdout.split()
    assert names == [CODEOWNERS]


def test_a_missing_local_only_file_is_a_hard_failure(world):
    """The case this script exists for. If the replica has already lost the file,
    syncing on top would make the loss permanent and invisible — so refuse."""
    up, rep = world
    _git(rep, "rm", "-q", CODEOWNERS)
    _git(rep, "commit", "-qm", "drop codeowners")
    with pytest.raises(SystemExit) as e:
        _run(rep)
    assert CODEOWNERS in str(e.value)
    assert "FAILED" in str(e.value)


def test_nothing_is_pushed_without_the_push_flag(world, capsys):
    up, rep = world
    assert _run(rep) == 0
    assert "nothing pushed" in capsys.readouterr().out
    heads = _git(rep, "for-each-ref", "--format=%(refname)", "refs/heads").stdout
    assert "chore/T-1-sync" not in heads


def test_push_creates_the_branch_on_the_replica(world):
    up, rep = world
    assert _run(rep, extra=("--push",)) == 0
    heads = _git(rep, "for-each-ref", "--format=%(refname)", "refs/heads").stdout
    assert "refs/heads/chore/T-1-sync" in heads


def test_the_signature_check_reads_headers_not_the_message():
    """The replica's ruleset requires a verified signature on every commit in the
    pull request, so an unsigned one is a wasted round trip at best.

    The check looks at HEADERS only. Searching the whole object would let a commit
    whose message discusses signing pass as signed — which is exactly the kind of
    message this sync writes."""
    unsigned = "tree t\nparent p\nauthor a\ncommitter c\n\nsubject\n"
    signed = "tree t\nparent p\ngpgsig -----BEGIN SSH SIGNATURE-----\n \n\nsubject\n"
    lying = "tree t\nparent p\nauthor a\n\nadd a gpgsig check to the sync\n"

    assert sync.signature_present(signed) is True
    assert sync.signature_present(unsigned) is False
    assert sync.signature_present(lying) is False, (
        "a commit MESSAGE mentioning gpgsig was read as a signature")


def test_the_remote_is_not_hardcoded():
    """A mirror's address is deployment configuration. This file ships to a public
    repository, so it must not name one."""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "QH_REPLICA_REMOTE" in src
    assert "github.com/" not in src, (
        "the sync script names a concrete remote — take it from the environment")


def test_every_local_only_entry_carries_its_reason():
    for path, why in sync.LOCAL_ONLY:
        assert path and why, "an entry with no reason will not survive review"
        assert len(why) > 20, f"{path}'s reason is too thin to be useful"
