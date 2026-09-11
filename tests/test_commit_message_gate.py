"""Refusing a commit message before the commit exists.

`check_repo_clean.py` scans COMMITTED messages, so by the time it finds a leak
the only remedy it can offer is rewriting history — which the script says
itself. That is not proportionate for a message nobody can un-publish, and it
arrives too late twice over: the commit is made, often pushed, sometimes
merged.

A real production alias reached a commit body that way twice. `--message`
closes the gap by scanning the DRAFT, wired to the `commit-msg` hook, where a
refusal costs one edit.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "hooks" / "commit-msg"


def _scanner():
    spec = importlib.util.spec_from_file_location(
        "check_repo_clean", ROOT / "scripts" / "check_repo_clean.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def scan(tmp_path, monkeypatch):
    """Scan a draft message against a known denylist, with no bot DB."""
    mod = _scanner()
    monkeypatch.setattr(
        mod, "_dynamic_terms_from_db",
        lambda: [(r"svc-prod-secret", "real target alias", True)])

    def run(text: str) -> int:
        p = tmp_path / "COMMIT_EDITMSG"
        p.write_text(text, encoding="utf-8")
        return mod.scan_message_file(str(p))
    return run


def test_a_message_naming_a_real_alias_is_refused(scan):
    assert scan("fix it\n\nbroken on svc-prod-secret today\n") == 1


def test_a_message_that_names_nothing_passes(scan):
    assert scan("fix it\n\nbroken on one production target today\n") == 0


def test_gits_own_comment_lines_are_not_scanned(scan):
    """`git commit` fills the file with commented scaffolding — including the
    branch and the file list. Scanning those would refuse commits for text the
    author never wrote and that never ships."""
    assert scan("fix it\n\n# On branch svc-prod-secret\n") == 0


def test_the_attribution_trailers_are_exempt_here_too(scan, monkeypatch):
    """A squash merge appends Co-authored-by with the owner's own name, which
    is legitimately on the denylist. The committed-message scan already exempts
    those trailers; the draft scan has to agree or the two gates disagree about
    the same text."""
    mod = _scanner()
    monkeypatch.setattr(
        mod, "_dynamic_terms_from_db",
        lambda: [(r"Jane Operator", "real person", True)])
    p = Path(str(mod.__file__)).parent  # unused, keeps the import honest
    assert p
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("fix it\n\nbody\n\nCo-authored-by: Jane Operator <j@example>\n")
        name = fh.name
    assert mod.scan_message_file(name) == 0


def test_a_missing_denylist_refuses_rather_than_passing(tmp_path, monkeypatch):
    """Fails closed. A scan that cannot load the real names proves nothing, and
    'clean' would be the one outcome a gate must never produce."""
    mod = _scanner()
    def boom():
        raise RuntimeError("no bot DB")
    monkeypatch.setattr(mod, "_dynamic_terms_from_db", boom)
    p = tmp_path / "m.txt"
    p.write_text("anything", encoding="utf-8")
    assert mod.scan_message_file(str(p)) == 2


# ---------------------------------------------------------------------------
# the hook that calls it
# ---------------------------------------------------------------------------

def test_the_hook_is_tracked_and_executable():
    """An untracked hook protects exactly one checkout. This one ships."""
    assert HOOK.exists(), "scripts/hooks/commit-msg must be in the repo"
    assert HOOK.stat().st_mode & 0o111, "hook must be executable"


def test_the_hook_hardcodes_no_absolute_checkout_path():
    """The repo is placeholder-only and gets exported to a public tree."""
    body = HOOK.read_text(encoding="utf-8")
    assert "/home/" not in body
    assert "git rev-parse --show-toplevel" in body


def test_the_hook_passes_the_message_file_through():
    body = HOOK.read_text(encoding="utf-8")
    assert "--message" in body and '"$1"' in body
