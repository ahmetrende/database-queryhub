"""Two settings that used to fail open now stop the process.

External review 2026-09-24:
- OPS-03: a `secrets.enc` that exists but cannot be read was logged and
  skipped, and the service started on whatever the plaintext environment still
  held. `QH_SECRETS_PLAINTEXT_FALLBACK=1` keeps the old behavior on purpose,
  for the move from a plaintext env file to the encrypted one.
- SEC-07: a `WEB_SESSION_SECRET` override was used at any length. A key shorter
  than HS256's 32 bytes is refused, at startup and at every use.
"""
import logging

import pytest

from queryhub import config as cfg
from queryhub.web import sessions


@pytest.fixture
def broken_secrets(tmp_path, monkeypatch):
    path = tmp_path / "secrets.enc"
    path.write_text("SLBOT_SECRETS_v1\nnot-a-fernet-token\n")
    path.chmod(0o600)
    monkeypatch.setenv("SECRETS_ENC_PATH", str(path))
    monkeypatch.delenv("QH_SECRETS_PLAINTEXT_FALLBACK", raising=False)
    return path


def test_a_secrets_file_that_cannot_be_read_stops_the_process(broken_secrets):
    with pytest.raises(RuntimeError) as e:
        cfg._maybe_load_encrypted_secrets()
    assert str(broken_secrets) in str(e.value)
    assert "QH_SECRETS_PLAINTEXT_FALLBACK" in str(e.value)


def test_the_fallback_is_a_deliberate_setting(broken_secrets, monkeypatch, caplog):
    monkeypatch.setenv("QH_SECRETS_PLAINTEXT_FALLBACK", "1")
    with caplog.at_level(logging.ERROR):
        cfg._maybe_load_encrypted_secrets()
    assert "Falling back to plaintext env" in caplog.text


def test_no_secrets_file_is_still_a_plaintext_install(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_ENC_PATH", str(tmp_path / "absent.enc"))
    cfg._maybe_load_encrypted_secrets()


def test_the_repair_tool_does_not_import_config():
    """With the file unreadable, config refuses to import, so the tool that
    rewrites the file must not need it."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("scripts/manage_env_secrets.py", "src/queryhub/secrets_store.py",
                "src/queryhub/crypto.py", "src/queryhub/__init__.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                 for a in n.names}
        mods = {getattr(n, "module", None) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert "config" not in names and not any(m and m.endswith("config") for m in mods), rel


# --- the session signing key ---------------------------------------------------


def test_a_short_override_is_refused(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SECRET", "x" * 31)
    with pytest.raises(RuntimeError) as e:
        sessions.check_signing_secret()
    assert "31 bytes" in str(e.value)


def test_an_override_of_32_bytes_is_used(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SECRET", "y" * 32)
    assert sessions._signing_secret() == b"y" * 32


def test_no_override_derives_32_bytes_from_the_master_key(monkeypatch):
    monkeypatch.delenv("WEB_SESSION_SECRET", raising=False)
    assert len(sessions._signing_secret()) == 32


def test_the_web_app_will_not_start_on_a_short_key(monkeypatch):
    from fastapi.testclient import TestClient

    from queryhub import db
    from queryhub.web import app as web_app
    monkeypatch.setattr(db, "init_pool", lambda: None)
    monkeypatch.setenv("WEB_SESSION_SECRET", "short")
    with pytest.raises(RuntimeError):
        with TestClient(web_app.create_app()):
            pass
