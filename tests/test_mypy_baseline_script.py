"""The mypy gate's bookkeeping, checked without running mypy.

scripts/check_mypy_baseline.py keys each error by file, code and message with
the line number dropped, and fails only when a key occurs more often than the
committed baseline allows. These pin what makes that a gate rather than noise:
an edit that only MOVES an error passes, a new error fails and names its line,
a fixed one passes and says the baseline can be tightened, and a run that
cannot be trusted is refused instead of compared.
"""
from __future__ import annotations

import importlib.util
import subprocess
import tomllib
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_mypy_baseline", ROOT / "scripts" / "check_mypy_baseline.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

A = ('src/pkg/a.py:{}: error: Incompatible types in assignment (expression has '
     'type "int", variable has type "str")  [assignment]')
B = ('src/pkg/b.py:{}: error: Item "None" of "dict[str, Any] | None" has no '
     'attribute "get"  [union-attr]')
C = 'src/pkg/c.py:{}: error: Name "x" already defined on line {}  [no-redef]'
NOTE = 'src/pkg/b.py:{}: note: Left operand is of type "set[str] | None"'


def _output(*lines: str) -> str:
    """mypy's stdout for these lines, summary included."""
    errors = [ln for ln in lines if ": error: " in ln]
    files = len({ln.split(":", 1)[0] for ln in errors})
    summary = (f"Found {len(errors)} errors in {files} files (checked 9 source files)"
               if errors else "Success: no issues found in 9 source files")
    return "\n".join([*lines, summary]) + "\n"


def _baseline(*lines: str) -> Counter:
    errors, _ = gate.parse_output(_output(*lines))
    return Counter(key for key, _ in errors)


def _compare(baseline: Counter, *lines: str):
    errors, _ = gate.parse_output(_output(*lines))
    return gate.compare(errors, baseline)


def test_errors_are_keyed_without_line_numbers_and_notes_are_skipped():
    errors, stated = gate.parse_output(_output(A.format(10), NOTE.format(3),
                                               B.format(3)))
    assert stated == 2
    assert [line for _, line in errors] == [10, 3]
    assert errors[0][0] == (
        "src/pkg/a.py", "assignment",
        'Incompatible types in assignment (expression has type "int", '
        'variable has type "str")')


def test_a_moved_error_is_not_new():
    baseline = _baseline(A.format(10), B.format(3))
    new, new_count, fixed = _compare(baseline, A.format(31), B.format(4))
    assert (new, new_count, fixed) == ({}, 0, Counter())


def test_a_line_quoted_inside_the_message_moves_too():
    """`already defined on line 12` carries a line number of its own, and it
    shifts with unrelated edits exactly like the error's."""
    baseline = _baseline(C.format(20, 12))
    assert _compare(baseline, C.format(25, 17)) == ({}, 0, Counter())


def test_a_new_error_fails_with_its_line_number():
    baseline = _baseline(A.format(10))
    new, new_count, fixed = _compare(baseline, A.format(10), B.format(7))
    assert new_count == 1
    assert list(new.values()) == [[7]]
    assert not fixed


def test_another_copy_of_a_known_error_is_new():
    """Counts, not presence: the baseline allows one of these in a.py, so a
    second is new even though the message is already listed."""
    baseline = _baseline(A.format(10))
    new, new_count, _ = _compare(baseline, A.format(10), A.format(50))
    assert new_count == 1
    assert list(new.values()) == [[10, 50]]      # both shown: either may be it


def test_a_fixed_error_passes_and_is_reported():
    baseline = _baseline(A.format(10), B.format(3))
    new, new_count, fixed = _compare(baseline, A.format(12))
    assert (new, new_count) == ({}, 0)
    assert list(fixed.elements()) == [gate.make_key(
        "src/pkg/b.py", "union-attr",
        'Item "None" of "dict[str, Any] | None" has no attribute "get"')]


def test_the_baseline_file_round_trips():
    errors, _ = gate.parse_output(_output(A.format(1), A.format(2), B.format(3)))
    text = gate.format_baseline([key for key, _ in errors], "9.9.9")
    counts, version = gate.load_baseline(text)
    assert version == "9.9.9"
    assert counts == Counter(key for key, _ in errors)


def test_a_run_that_cannot_be_trusted_is_refused():
    errors, stated = gate.parse_output(_output(A.format(1)))
    assert gate.run_problem(1, "", errors, stated) is None
    # A crash, or a blocking error such as a syntax error.
    assert gate.run_problem(2, "", errors, stated)
    # No summary: a truncated run.
    assert gate.run_problem(1, "", errors, None)
    # The summary counts an error the parser did not see.
    assert gate.run_problem(1, "", errors, stated + 1)
    # A misspelled config option: mypy warns on stderr and ignores it.
    assert gate.run_problem(1, "pyproject.toml: [mypy]: Unrecognized option",
                            errors, stated)
    assert gate.run_problem(0, "", errors, stated)


def _run(monkeypatch, tmp_path, stdout, *, returncode=None, stderr="",
         args=()):
    """main() against a fake mypy run and a baseline under tmp_path."""
    if returncode is None:
        returncode = 1 if ": error: " in stdout else 0
    done = subprocess.CompletedProcess([], returncode, stdout, stderr)
    monkeypatch.setattr(gate, "subprocess",
                        SimpleNamespace(run=lambda *a, **k: done))
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "BASELINE", tmp_path / "mypy_baseline.txt")
    monkeypatch.setattr(gate, "_mypy_version", lambda: "9.9.9")
    return gate.main(list(args))


def test_exit_codes_end_to_end(monkeypatch, tmp_path, capsys):
    before = _output(A.format(10), B.format(3))
    assert _run(monkeypatch, tmp_path, before, args=["--update"]) == 0

    assert _run(monkeypatch, tmp_path, _output(A.format(40), B.format(3))) == 0
    assert "2 errors, baseline 2, 0 new" in capsys.readouterr().out

    assert _run(monkeypatch, tmp_path,
                _output(A.format(10), B.format(3), C.format(8, 2))) == 1
    assert "src/pkg/c.py:8: error:" in capsys.readouterr().out

    assert _run(monkeypatch, tmp_path, _output(A.format(10))) == 0
    assert "--update" in capsys.readouterr().out

    assert _run(monkeypatch, tmp_path, "", returncode=2,
                stderr="Traceback (most recent call last):") == 2


def test_every_mypy_override_names_a_module_that_exists():
    """An override for a module that is not there does nothing and says
    nothing, so a rename would quietly drop the strict modules out of the
    gate."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    overrides = config["tool"]["mypy"].get("overrides", [])
    assert overrides, "the strict override in pyproject.toml is gone"
    for override in overrides:
        modules = override["module"]
        for name in [modules] if isinstance(modules, str) else modules:
            base = ROOT / "src" / Path(*name.removesuffix(".*").split("."))
            assert (base.with_suffix(".py").is_file()
                    or (base / "__init__.py").is_file()), name


# --- the two bugs the strict check found ------------------------------------


def test_the_local_provider_has_no_redirect_leg(monkeypatch):
    """/api/auth/local/start raised AttributeError (a 500): local signs in by
    password POST and has neither start() nor exchange()."""
    import pytest

    from queryhub.web import auth_providers, routes_auth

    class _Local(auth_providers.LocalPassword):
        @staticmethod
        def enabled():
            return True
    monkeypatch.setattr(auth_providers, "get_provider", lambda name: _Local())
    for call in (lambda: routes_auth.auth_start("local"),
                 lambda: routes_auth.auth_callback("local", None, code="c", state="s")):
        with pytest.raises(Exception) as e:
            call()
        assert getattr(e.value, "status_code", None) == 404


def test_an_aws_secret_that_is_json_but_not_an_object_says_so(monkeypatch):
    import json
    import sys
    import types

    import pytest

    from queryhub import secrets_providers as sp

    class _Client:
        def __init__(self, raw):
            self.raw = raw

        def get_secret_value(self, SecretId):
            return {"SecretString": self.raw}

    prov = sp.AwsSecretsManagerProvider()
    monkeypatch.setattr(prov, "_cache_ttl", lambda: 0.0)
    row = {"id": 1, "secrets_ref": {"secret_id": "dba/prod-main", "region": "eu-central-1"}}
    for raw in ('"pw"', "[1, 2]", "null", json.dumps({"ro": "user:pw"})):
        monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(
            client=lambda *a, _raw=raw, **k: _Client(_raw)))
        with pytest.raises(sp.SecretsProviderError):
            prov.get_credentials(row, "ro")
