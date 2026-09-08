"""How the replica's work lands here: as a patch, on a branch, without moving
this checkout.

The first import this script performed would have reverted three releases of
one file. Its apply step took the replica's WHOLE copy of every touched path,
and a copy taken from a tree that branched off three releases ago is three
releases old. A patch keeps what moved here; a conflict is the signal that a
human has to look, not noise to route around.
"""
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import import_from_replica as imp  # noqa: E402


def _git(repo: Path, *args: str, input: str | None = None) -> str:
    out = subprocess.run(("git", *args), cwd=str(repo), capture_output=True,
                         text=True, input=input)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _commit(repo: Path, msg: str, **files: str) -> str:
    for name, body in files.items():
        (repo / name).write_text(body, encoding="utf-8")
        _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository that is both 'here' and 'the replica': the replica's state
    is just another ref, which is exactly what FETCH_HEAD is to the script."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "commit.gpgsign", "false")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "user.email", "t@x")
    base = _commit(r, "base", **{"f.py": "a\nb\nc\n", "other.py": "x\n"})
    monkeypatch.setattr(imp, "ROOT", r)
    return r, base


def _their_ref(repo: Path, base: str, **files: str) -> str:
    _git(repo, "checkout", "-q", "-b", "replica", base)
    sha = _commit(repo, "their change", **files)
    _git(repo, "checkout", "-q", "main")
    return sha


def test_a_clean_patch_keeps_what_moved_here(repo):
    r, base = repo
    theirs = _their_ref(r, base, **{"f.py": "a\nb\nc\nd\n"})          # they append
    _commit(r, "ours since the sync", **{"f.py": "z\na\nb\nc\n"})       # we prepend
    sha, replaced = imp.apply_theirs(base, theirs, ["f.py"], "imp", "import\n")
    assert replaced == []
    assert _git(r, "show", f"{sha}:f.py") == "z\na\nb\nc\nd\n"         # both survive


def test_a_conflict_falls_back_to_their_file_and_names_it(repo):
    r, base = repo
    theirs = _their_ref(r, base, **{"f.py": "a\nb\nTHEIRS\n"})
    _commit(r, "ours since the sync", **{"f.py": "a\nb\nOURS\n", "other.py": "y\n"})
    sha, replaced = imp.apply_theirs(base, theirs, ["f.py"], "imp", "import\n")
    assert replaced == ["f.py"]                                          # said so
    assert _git(r, "show", f"{sha}:f.py") == "a\nb\nTHEIRS\n"           # their copy
    assert _git(r, "show", f"{sha}:other.py") == "y\n"                  # untouched path kept


def test_this_checkout_never_moves(repo):
    r, base = repo
    theirs = _their_ref(r, base, **{"f.py": "a\nb\nc\nd\n"})
    head_before = _git(r, "rev-parse", "HEAD").strip()
    imp.apply_theirs(base, theirs, ["f.py"], "imp", "import\n")
    assert _git(r, "branch", "--show-current").strip() == "main"
    assert _git(r, "rev-parse", "HEAD").strip() == head_before
    assert _git(r, "status", "--porcelain").strip() == ""
    assert _git(r, "rev-parse", "--verify", "imp").strip()               # the branch exists
    assert not any(p.name.startswith("import-replica-") for p in r.parent.iterdir())


def test_the_source_no_longer_switches_branches_in_place():
    src = (SCRIPTS / "import_from_replica.py").read_text(encoding="utf-8")
    assert 'git("checkout", "-q", "-b"' not in src
    assert "worktree" in src and "--3way" in src
