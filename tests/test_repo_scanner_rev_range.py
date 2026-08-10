"""The commit-message scan must cover what an event introduces, not all history.

The scanner grandfathers upstream's pre-gate messages through a recorded SHA,
because rewriting 391 of 449 commits to clean a repository that is never
published buys nothing. That reasoning was written for two repositories: upstream
(has the anchor) and the published export (shares no commits with upstream, and
its messages are clean either way).

It missed a third consumer. A downstream replica carries SOME of upstream's old
commits but NOT the anchor, so `_message_scan_range()` fell back to `HEAD` and the
scan re-flagged five 2026-05 messages nobody in the pull request wrote. The gate
went permanently red on a repository whose ruleset forbids the force push that
would fix it — a gate that cannot go green is a gate people route around.

`QH_SCAN_REV_RANGE` already existed for this. These tests pin that it works in
BOTH directions, because a range that silences everything would be worse than the
red it replaced.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

_PATH = (pathlib.Path(__file__).resolve().parents[1]
         / "scripts" / "check_repo_clean.py")
_CI = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

# Same reasoning as test_repo_scanner_trailers.py: the scanner is an operator
# tool, deliberately not shipped to the published repository, and the absence is
# handled per test so `-m integration` DESELECTS rather than reporting a skip.
_HAVE = _PATH.exists()
upstream_only = pytest.mark.skipif(
    not _HAVE, reason="check_repo_clean.py is upstream-only")

if _HAVE:
    _SPEC = importlib.util.spec_from_file_location("check_repo_clean_rr", _PATH)
    crc = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(crc)
else:
    crc = None

# Built at runtime, never written as a literal. This file is itself scanned, and
# the shape below is one the static list catches — spelling it out would make the
# test fail the gate it tests.
LEAK = "10" + ".1.2.3"


def _git(cwd, *args):
    return subprocess.run(("git", *args), cwd=str(cwd), capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """Three commits: clean base, leaky middle, clean tip.

    A real repository, because the thing under test is a `git log` range.
    """
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    _git(r, "config", "commit.gpgsign", "false")

    (r / "a.txt").write_text("nothing sensitive\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    base = _git(r, "rev-parse", "HEAD")

    (r / "a.txt").write_text("still nothing\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", f"old work\n\nran against {LEAK} while testing\n")
    leaky = _git(r, "rev-parse", "HEAD")

    (r / "a.txt").write_text("clean change\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "recent work\n\nnothing to hide here\n")

    # main() assigns the module-global SCAN_ROOT; restore it so ordering between
    # tests cannot matter.
    monkeypatch.setattr(crc, "SCAN_ROOT", crc.SCAN_ROOT, raising=False)
    monkeypatch.delenv("QH_SCAN_REV_RANGE", raising=False)
    return r, base, leaky


def _scan(root, capsys, rev_range=None, monkeypatch=None):
    if rev_range is not None:
        monkeypatch.setenv("QH_SCAN_REV_RANGE", rev_range)
    code = crc.main(["--static-only", "--root", str(root)])
    return code, capsys.readouterr().out


# ---------------------------------------------------------------------------
# the defect: no anchor, so the default range is all of history
# ---------------------------------------------------------------------------

@upstream_only
def test_without_a_range_the_old_commit_is_flagged(repo, capsys, monkeypatch):
    """The replica's situation exactly: the anchor SHA is absent, so the scan
    falls back to HEAD and fails on history the pull request did not write."""
    root, _base, _leaky = repo
    assert crc.GRANDFATHERED_THROUGH not in _git(root, "log", "--format=%H")
    code, out = _scan(root, capsys, monkeypatch=monkeypatch)
    assert code != 0
    assert "commit message(s)" in out
    assert "RFC1918" in out, out


@upstream_only
def test_a_range_past_the_old_commit_is_clean(repo, capsys, monkeypatch):
    """What the CI job now passes: only the commits this event introduces."""
    root, _base, leaky = repo
    code, out = _scan(root, capsys, f"{leaky}..HEAD", monkeypatch)
    assert code == 0, out
    assert "Scanning 1 commit message(s)." in out, out


# ---------------------------------------------------------------------------
# the other half: narrowing must not blind the gate
# ---------------------------------------------------------------------------

@upstream_only
def test_a_range_that_includes_the_leak_still_fails(repo, capsys, monkeypatch):
    """If this ever passes, the range has become a way to silence the scan."""
    root, base, _leaky = repo
    code, out = _scan(root, capsys, f"{base}..HEAD", monkeypatch)
    assert code != 0, "a leak inside the scanned range was not reported"
    assert "RFC1918" in out


@upstream_only
def test_a_leak_in_the_new_commit_is_caught(repo, capsys, monkeypatch):
    """The case that matters most: the range is narrow AND the leak is new."""
    root, _base, _leaky = repo
    before = _git(root, "rev-parse", "HEAD")
    (root / "a.txt").write_text("newer\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"new work\n\ndeployed to {LEAK}\n")
    code, out = _scan(root, capsys, f"{before}..HEAD", monkeypatch)
    assert code != 0, "a leak in the pull request's own commit was missed"
    assert "RFC1918" in out


@upstream_only
def test_an_empty_range_falls_back_to_the_default(repo, capsys, monkeypatch):
    """The workflow sets nothing when an event has no base (a new branch reports
    an all-zero SHA). Empty must mean "no opinion", not "scan nothing"."""
    root, _base, _leaky = repo
    code, out = _scan(root, capsys, "", monkeypatch)
    assert code != 0, "an empty range silenced the scan instead of deferring"
    assert "RFC1918" in out


# ---------------------------------------------------------------------------
# the workflow half — cheap, and the shell is where the guard lives
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _CI.exists(), reason="workflow is upstream-only")
def test_the_workflow_derives_a_base_for_both_event_kinds():
    src = _CI.read_text(encoding="utf-8")
    assert "QH_SCAN_REV_RANGE=" in src, "the leak gate scans all history again"
    assert "pull_request.base.sha" in src
    assert "github.event.before" in src, (
        "a push to the default branch would re-scan history and go red after merge")
    assert "0000000000000000000000000000000000000000" in src, (
        "a new branch reports an all-zero base; without the guard the range is "
        "nonsense and git fails")
