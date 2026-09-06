"""The assertion section of the live security smoke test.

The script itself needs a running instance, so what is worth pinning here is
the one branch that decides whether its verdict means anything: when the seam
is switched off, every assertion is refused for a reason that has nothing to
do with the checks, and reporting those as PASS would be a suite calling
itself green while proving nothing.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "security_smoke.py"
_SPEC = importlib.util.spec_from_file_location("security_smoke", _SCRIPT)
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


class _RefusingClient:
    """Answers 401 to everything and records that it was called."""

    def __init__(self):
        self.calls = 0

    def _resp(self):
        self.calls += 1
        return type("R", (), {"status_code": 401, "text": ""})()

    def get(self, *a, **k):
        return self._resp()

    def request(self, *a, **k):
        return self._resp()


@pytest.fixture(autouse=True)
def _clean_results(monkeypatch):
    monkeypatch.setattr(smoke, "_results", [])
    monkeypatch.setattr(smoke, "_skipped", [])


def _setting(values):
    return lambda k, d=None: values.get(k, d)


def test_a_disabled_seam_is_skipped_not_passed(monkeypatch):
    from queryhub import config as cfg
    monkeypatch.setattr(cfg, "get_setting",
                        _setting({"idp_assertion_enabled": "off"}))
    client = _RefusingClient()

    smoke.run_idp_assertion(client)

    assert smoke._results == [], "a disabled seam must bank no passes"
    assert len(smoke._skipped) == 1
    assert "idp_assertion_enabled is off" in smoke._skipped[0][1]
    assert client.calls == 0, "nothing should have been sent"


def test_keyless_checks_run_and_the_rest_are_skipped(monkeypatch):
    """Without the panel's private key only the outsider checks are possible;
    the others must be reported as not run rather than quietly dropped."""
    from queryhub import config as cfg
    monkeypatch.setattr(cfg, "get_setting", _setting({
        "idp_assertion_enabled": "on",
        "idp_public_keys": '{"k1": "-----BEGIN PUBLIC KEY-----\\nAAA\\n-----END PUBLIC KEY-----"}',
    }))
    monkeypatch.delenv("QH_IDP_SIGNING_KEY", raising=False)
    client = _RefusingClient()

    smoke.run_idp_assertion(client)

    names = [name for _, name, _ in smoke._results]
    assert any("alg=none" in n for n in names)
    assert any("foreign key" in n for n in names)
    assert any("HS256" in n for n in names)
    assert all(ok for ok, _, _ in smoke._results), "a 401 is the secure answer"
    assert any("QH_IDP_SIGNING_KEY" in reason for _, reason in smoke._skipped)
