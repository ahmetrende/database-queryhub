"""The tool that has to prove the access migration changed nothing.

`scripts/access_snapshot.py` freezes what every principal may do, and the
migration is only allowed to proceed while two snapshots agree. That makes the
comparison itself load-bearing: a diff that quietly misses a case would sign off
on a permission change nobody saw. So the cases it must not miss are pinned
here — a key that vanished, a key that appeared, a tier that moved, and the
ordering that decides what a truncated report shows first.

None of this touches a database. That is the point of the split these tests
also guard: capture needs the bot DB, comparing two captures does not, and the
half that gates a deploy has to run in CI.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location(
        "access_snapshot", SCRIPTS / "access_snapshot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["access_snapshot"] = mod
    spec.loader.exec_module(mod)
    return mod


snap = _load()


def _snapshot(access=None, approve=None, flags=None, visible=None, backend="legacy"):
    return {"backend": backend, "captured_at": "2026-01-01T00:00:00+00:00",
            "flags": flags or {}, "visible": visible or {},
            "access": access or {}, "approve": approve or {}}


# --- what the diff must catch ----------------------------------------------


def test_identical_snapshots_report_nothing():
    a = _snapshot(access={"u|1|db": {"tier": "ro", "can_db": True}})
    assert snap.diff(a, json.loads(json.dumps(a))) == []


def test_a_tier_that_moved_is_reported_with_both_values():
    before = _snapshot(access={"u|1|db": {"tier": "ro"}})
    after = _snapshot(access={"u|1|db": {"tier": "rw"}})
    (line,) = snap.diff(before, after)
    assert "u|1|db" in line and "'ro'" in line and "'rw'" in line


def test_access_that_vanished_is_reported():
    """The failure mode that matters most: somebody silently lost access."""
    before = _snapshot(access={"u|1|db": {"tier": "rw"}})
    (line,) = snap.diff(before, _snapshot())
    assert "disappeared" in line and "u|1|db" in line


def test_access_that_appeared_is_reported():
    """And its twin: a migration that hands out access nobody granted."""
    after = _snapshot(access={"u|1|db": {"tier": "ddl"}})
    (line,) = snap.diff(_snapshot(), after)
    assert "appeared" in line and "ddl" in line


def test_every_section_is_compared_not_just_access():
    before = _snapshot(flags={"u": {"is_admin": False}},
                       visible={"u": [1]},
                       access={"u|1|db": {"tier": "ro"}},
                       approve={"a|u|1|ro": True})
    after = _snapshot(flags={"u": {"is_admin": True}},
                      visible={"u": [1, 2]},
                      access={"u|1|db": {"tier": "rw"}},
                      approve={"a|u|1|ro": False})
    sections = {line.split(":")[0] for line in snap.diff(before, after)}
    assert sections == {"flags", "visible", "access", "approve"}


def test_a_lost_key_is_reported_before_a_changed_one():
    """Truncation decides what a human reads. A person who lost access has to
    be on the first screen, not below thirty tier changes."""
    before = _snapshot(access={f"u|{i}|db": {"tier": "ro"} for i in range(5)}
                       | {"gone|9|db": {"tier": "ddl"}})
    after = _snapshot(access={f"u|{i}|db": {"tier": "rw"} for i in range(5)})
    assert "disappeared" in snap.diff(before, after)[0]


def test_the_limit_truncates_and_zero_means_all():
    before = _snapshot(access={f"u|{i}|db": {"tier": "ro"} for i in range(50)})
    after = _snapshot(access={f"u|{i}|db": {"tier": "rw"} for i in range(50)})
    assert len(snap.diff(before, after, limit=10)) == 10
    assert len(snap.diff(before, after, limit=0)) == 50


# --- the gate has to run where the database does not ------------------------


def test_comparing_two_files_needs_no_database(tmp_path):
    """`compare` is what gates the cutover. If it needed BOT_DB_* it could not
    run in CI, and the evidence would only ever be checked on the one box that
    can also change the answer."""
    a = tmp_path / "a.json"
    a.write_text(json.dumps(_snapshot(access={"u|1|db": {"tier": "ro"}})),
                 encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "access_snapshot.py"), "compare",
         str(a), str(a)],
        capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert done.returncode == 0, done.stderr
    assert "identical" in done.stdout


def test_a_difference_fails_the_gate(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(_snapshot(access={"u|1|db": {"tier": "ro"}})),
                 encoding="utf-8")
    b.write_text(json.dumps(_snapshot(access={"u|1|db": {"tier": "ddl"}})),
                 encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "access_snapshot.py"), "compare",
         str(a), str(b)],
        capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert done.returncode == 1
    assert "1 difference(s)" in done.stdout


# --- the backends -----------------------------------------------------------


def test_the_new_backend_answers_from_the_new_resolver():
    """The whole point of the diff. If the new backend inherited even one
    answer from the legacy one, that answer would agree with itself and the
    green result would prove nothing about it.

    `row_caps` is the deliberate exception, documented on the class: row limits
    are not part of this rewrite, so there is no new implementation to compare
    and delegating makes no claim either way.
    """
    import inspect
    questions = ["tier", "grant_source", "can_target", "can_database",
                 "visible_targets", "auto_tier", "can_approve", "flags"]
    for name in questions:
        assert name in vars(snap.NewBackend), f"{name} falls through to legacy"
        body = inspect.getsource(getattr(snap.NewBackend, name))
        assert "self.access" in body, f"{name} does not ask the new resolver"
    assert "row_caps" in inspect.getsource(snap.NewBackend.flags)
    assert "self.row_limits" in inspect.getsource(snap.NewBackend.flags)


def test_the_two_words_the_models_spell_differently_are_translated_once():
    """The old resolver says `admin_or_bypass` for both an admins row and a
    bypass flag; the new one separates them, because only the first also sees
    disabled targets.

    That translation started here, as a comparison concern. It moved into
    `access.legacy_shape` when `teams.py` began delegating, because the product
    then needed it too — and a mapping written twice is a mapping that can
    disagree with itself. Pin the single copy.
    """
    import inspect
    src = (Path(snap.__file__).parent.parent / "src" / "queryhub"
           / "access.py").read_text(encoding="utf-8")
    assert src.count('source = "admin_or_bypass"') == 1     # the code, once
    assert "def legacy_shape(" in src
    # and the backend goes through it rather than repeating it
    body = inspect.getsource(snap.NewBackend.grant_source)
    assert "legacy_shape" in body
    assert "admin_or_bypass" not in body


def test_the_tier_authority_is_the_per_database_one():
    """effective_grant_for_user()["mode"] is the max tier across the UNION of
    databases; using it here would record RW on a database only a read grant
    covers. Pin the call the backend actually makes."""
    src = (SCRIPTS / "access_snapshot.py").read_text(encoding="utf-8")
    assert "effective_mode_for_database" in src
    assert 'g["mode"]' not in src


def test_the_unlisted_sentinel_is_asked_about():
    """A name no grant mentions is how the wildcard is measured: a grant with
    no database list must cover it, a grant that names databases must not."""
    src = (SCRIPTS / "access_snapshot.py").read_text(encoding="utf-8")
    assert "UNLISTED" in src and "names.add(UNLISTED)" in src
