"""The replica is rebuilt, not merged — so work written there has to come back.

`sync_downstream_replica.py` writes a tree. That is safe while the replica is
only read, and stops being safe the moment somebody develops in it, which is
what the IdP integration does: their pull request merges downstream, not here.
A rebuilt tree that lacks their files is a perfectly valid tree, so git would
report success and the work would be gone — no error, no conflict, nothing to
notice.

Two halves close that: every sync now plants an `Upstream-Commit:` trailer, and
the sync REFUSES while the replica carries commits after it. The import script
is what makes the refusal actionable.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


imp = _load("import_from_replica")
sync = _load("sync_downstream_replica")


# --- the baseline marker ----------------------------------------------------

LOG = ("aaa1\x00their second commit\n\nbody\x1e"
       "bbb2\x00their first commit\n\x1e"
       "ccc3\x00Sync the replica with master\n\nUpstream-Commit: deadbeef\n\x1e"
       "ddd4\x00an older sync\n\nUpstream-Commit: cafe\n\x1e")


@pytest.mark.parametrize("mod", [imp, sync])
def test_the_newest_sync_is_the_baseline(mod, monkeypatch):
    fn = getattr(mod, "newest_marker", None) or mod._newest_marker
    monkeypatch.setattr(mod, "git", lambda *a, **k: LOG)
    assert fn("FETCH_HEAD") == "ccc3"


@pytest.mark.parametrize("mod", [imp, sync])
def test_a_replica_that_never_took_a_sync_has_no_baseline(mod, monkeypatch):
    fn = getattr(mod, "newest_marker", None) or mod._newest_marker
    monkeypatch.setattr(mod, "git", lambda *a, **k: "aaa1\x00just their work\n\x1e")
    assert fn("FETCH_HEAD") is None


def test_the_sync_plants_the_marker_without_being_asked():
    """A marker somebody has to remember to type is a marker that will be
    missing on the sync that needed it."""
    src = (SCRIPTS / "sync_downstream_replica.py").read_text(encoding="utf-8")
    assert "if TRAILER not in msg:" in src
    assert 'msg.rstrip("\\n") + f"\\n\\n{TRAILER} {base}\\n"' in src


def test_the_sync_refuses_by_default_and_names_the_way_out():
    src = (SCRIPTS / "sync_downstream_replica.py").read_text(encoding="utf-8")
    assert "if not args.discard_downstream:" in src        # refusal is the default
    assert "REFUSED" in src
    assert "import_from_replica.py" in src                 # tells you what to run


# --- what comes back --------------------------------------------------------

def test_the_replicas_own_files_do_not_travel_upstream(monkeypatch):
    """CODEOWNERS names reviewers of the downstream org. Importing it would push
    that file into every other consumer of this repo, including the public one —
    which is the reason this cannot be a cherry-pick."""
    monkeypatch.setattr(imp, "git", lambda *a, **k:
                        "src/queryhub/web/idp_assertion.py\n"
                        ".github/CODEOWNERS\n"
                        "migrations/101_idp_assertion_jti.sql\n")
    assert imp.changed("base", "ref") == [
        "src/queryhub/web/idp_assertion.py",
        "migrations/101_idp_assertion_jti.sql",
    ]
    assert ".github/CODEOWNERS" in {p for p, _ in sync.LOCAL_ONLY}


def test_both_directions_read_one_list():
    # Two lists would eventually disagree about which files are the replica's.
    src = (SCRIPTS / "import_from_replica.py").read_text(encoding="utf-8")
    assert "from sync_downstream_replica import LOCAL_ONLY" in src


def test_every_author_is_kept(monkeypatch):
    commits = [
        {"sha": "a" * 40, "name": "Their Dev", "email": "dev@example.com",
         "subject": "add the assertion verifier"},
        {"sha": "b" * 40, "name": "Their Dev", "email": "dev@example.com",
         "subject": "close the empty-list hole"},
        {"sha": "c" * 40, "name": "Someone Else", "email": "two@example.com",
         "subject": "fix the jti sweep"},
    ]
    # De-duplicated, order preserved: an import that silently reattributes
    # someone's work is worse than no tool.
    assert imp.coauthors(commits) == [
        "Co-Authored-By: Their Dev <dev@example.com>",
        "Co-Authored-By: Someone Else <two@example.com>",
    ]
    msg = imp.message(commits, "d" * 40, "e" * 40)
    assert "3 commit(s)" in msg
    for c in commits:
        assert c["subject"] in msg
        assert c["sha"][:8] in msg


def test_the_commit_message_says_where_the_work_came_from():
    msg = imp.message([{"sha": "a" * 40, "name": "N", "email": "e@x",
                        "subject": "s"}], "b" * 40, "c" * 40)
    assert msg.splitlines()[0] == "import the replica's own commits"
    assert "bbbbbbbb..cccccccc" in msg
