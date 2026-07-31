"""Master key rotation: the key ring, and the transition window it creates.

Before this, crypto.py built one Fernet from one key and there was no way to
rotate without downtime and a hand-written re-encryption pass. An operator who
needed to rotate — exposure, staff change, a 90-day policy — had no safe path,
so in practice the key never moved.

The file is now a key ring: line 1 is the primary and everything new is
encrypted with it, while later lines still decrypt. What these tests protect is
the WINDOW, because that is where a rotation goes wrong:

  * old ciphertext must keep decrypting after a new key is prepended. If it did
    not, step 2 (prepend + restart) would be an outage.
  * new writes must use the primary, or step 3 re-encrypts nothing and step 4
    destroys data.
  * the env-secrets file must stay readable mid-rotation too. It holds the
    credentials the bot needs to BOOT, so reading it with the primary alone
    would make the rotation fail at the worst possible moment — and it would
    have: secrets_store built its own single Fernet from the primary.
  * a single-key file must behave exactly as before, since every existing
    install has one.
"""
import pytest
from cryptography.fernet import Fernet

from queryhub import crypto


@pytest.fixture
def keyfile(tmp_path, monkeypatch):
    """A master key file whose contents a test can rewrite, with the module
    cache dropped on every change (the real rotation restarts the process; a
    test cannot, so it must invalidate by hand)."""
    path = tmp_path / "master.key"

    def write(*keys: bytes, mode: int = 0o600):
        path.write_bytes(b"\n".join(keys) + b"\n")
        path.chmod(mode)
        crypto.reset_cache()
        return path

    monkeypatch.setenv("MASTER_KEY_PATH", str(path))
    crypto.reset_cache()
    yield write
    crypto.reset_cache()


KEY_A = Fernet.generate_key()
KEY_B = Fernet.generate_key()
KEY_C = Fernet.generate_key()


# --------------------------------------------------- backwards compatibility


def test_a_single_key_file_works_exactly_as_before(keyfile):
    keyfile(KEY_A)
    token = crypto.encrypt("hunter2")
    assert crypto.decrypt(token) == "hunter2"
    assert crypto.read_keys() == [KEY_A]


def test_a_trailing_newline_or_whitespace_is_tolerated(keyfile, tmp_path):
    """The old reader did `.strip()` on the whole file. Existing key files were
    written by hand and by install.sh, so they have all the usual variations."""
    path = tmp_path / "master.key"
    path.write_bytes(b"  " + KEY_A + b"  \n\n")
    path.chmod(0o600)
    crypto.reset_cache()
    assert crypto.read_keys() == [KEY_A]
    assert crypto.decrypt(crypto.encrypt("x")) == "x"


# ------------------------------------------------------ the rotation window


def test_old_ciphertext_still_decrypts_after_the_new_key_is_prepended(keyfile):
    """Step 2 of the rotation. This is the property that makes it non-disruptive:
    if prepending a key broke existing ciphertext, every stored credential would
    become unreadable the moment the operator restarted."""
    keyfile(KEY_A)
    old_token = crypto.encrypt("db-password")

    keyfile(KEY_B, KEY_A)          # new primary, old key retained
    assert crypto.decrypt(old_token) == "db-password"


def test_new_writes_use_the_primary_key(keyfile):
    """Step 3 depends on this. If writes kept using the old key, the
    re-encryption pass would be a no-op and step 4 would delete the only key
    that could read the data."""
    keyfile(KEY_B, KEY_A)
    fresh = crypto.encrypt("new-secret")

    # Readable by the new key alone...
    assert Fernet(KEY_B).decrypt(fresh.encode()).decode() == "new-secret"
    # ...and NOT by the old one.
    with pytest.raises(Exception):
        Fernet(KEY_A).decrypt(fresh.encode())


def test_encrypted_with_primary_distinguishes_old_from_new(keyfile):
    """How the rotation script decides what still needs work, and what makes it
    resumable — an interrupted run can be repeated and skips what it finished."""
    keyfile(KEY_A)
    old = crypto.encrypt("v1")

    keyfile(KEY_B, KEY_A)
    new = crypto.encrypt("v2")

    assert crypto.encrypted_with_primary(old) is False
    assert crypto.encrypted_with_primary(new) is True


def test_a_three_key_ring_reads_ciphertext_from_any_generation(keyfile):
    """Two rotations in a row, or a slow one. Order in the file is
    newest-to-oldest but decryption must not care."""
    keyfile(KEY_A)
    t_a = crypto.encrypt("gen-a")
    keyfile(KEY_B, KEY_A)
    t_b = crypto.encrypt("gen-b")
    keyfile(KEY_C, KEY_B, KEY_A)
    t_c = crypto.encrypt("gen-c")

    assert crypto.decrypt(t_a) == "gen-a"
    assert crypto.decrypt(t_b) == "gen-b"
    assert crypto.decrypt(t_c) == "gen-c"


def test_removing_the_old_key_too_early_gives_an_actionable_error(keyfile):
    """Step 4 done before step 3 — the realistic mistake. The message has to
    name the cause, because the operator is now looking at a bot that cannot
    read its own credentials."""
    keyfile(KEY_A)
    old_token = crypto.encrypt("db-password")
    keyfile(KEY_B)                 # old key deleted without re-encrypting

    with pytest.raises(RuntimeError) as err:
        crypto.decrypt(old_token)
    msg = str(err.value)
    assert "rotation" in msg.lower()
    assert "restore" in msg.lower()


# ------------------------------------------------------------ file handling


def test_comments_and_blank_lines_are_ignored(keyfile, tmp_path):
    """So an operator can label which key is which during a rotation. That
    label is the difference between a confident step 4 and a nervous one."""
    path = tmp_path / "master.key"
    path.write_bytes(b"# rotated 2026-07-25, remove the line below after step 3\n"
                     + KEY_B + b"\n\n# previous key\n" + KEY_A + b"\n")
    path.chmod(0o600)
    crypto.reset_cache()
    assert crypto.read_keys() == [KEY_B, KEY_A]


def test_an_empty_or_comment_only_file_is_an_error_not_a_silent_default(keyfile,
                                                                       tmp_path):
    path = tmp_path / "master.key"
    path.write_bytes(b"# nothing here\n\n")
    path.chmod(0o600)
    crypto.reset_cache()
    with pytest.raises(ValueError, match="no keys"):
        crypto.read_keys()


def test_a_malformed_line_says_which_line(keyfile, tmp_path):
    """cryptography's own message does not say which of several lines is bad,
    and during a rotation that is the only thing you want to know."""
    path = tmp_path / "master.key"
    path.write_bytes(KEY_A + b"\nnot-a-valid-fernet-key\n")
    path.chmod(0o600)
    crypto.reset_cache()
    with pytest.raises(ValueError, match="Line 2"):
        crypto.fernet()


def test_a_permissive_key_file_is_still_refused(keyfile):
    """Pre-existing protection; the multi-key reader must not have dropped it."""
    keyfile(KEY_A, mode=0o644)
    with pytest.raises(PermissionError, match="too permissive"):
        crypto.read_keys()


def test_a_missing_key_file_still_explains_how_to_make_one(monkeypatch, tmp_path):
    monkeypatch.setenv("MASTER_KEY_PATH", str(tmp_path / "absent.key"))
    crypto.reset_cache()
    with pytest.raises(FileNotFoundError, match="Fernet.generate_key"):
        crypto.read_keys()


# -------------------------------------------- the env-secrets file, in-window


def test_the_secrets_file_stays_readable_after_prepending_a_key(keyfile, tmp_path,
                                                               monkeypatch):
    """The one that would have bitten hardest. This file holds SLACK_BOT_TOKEN
    and BOT_DB_PASSWORD — the credentials the process needs to start. It read
    itself with a single Fernet built from the primary, so at step 2 the bot
    would have failed to boot: the rotation meant to be non-disruptive would
    have taken the service down before anything was re-encrypted."""
    from queryhub import secrets_store

    secrets_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENC_PATH", str(secrets_path))

    keyfile(KEY_A)
    secrets_store.save({"SLACK_BOT_TOKEN": "xoxb-old"}, secrets_path)

    keyfile(KEY_B, KEY_A)          # step 2
    assert secrets_store.load(secrets_path) == {"SLACK_BOT_TOKEN": "xoxb-old"}


def test_rewriting_the_secrets_file_moves_it_to_the_primary_key(keyfile, tmp_path,
                                                               monkeypatch):
    """Step 3 for the file half: after a save it must be readable by the new key
    alone, so step 4 is safe."""
    from queryhub import secrets_store

    secrets_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENC_PATH", str(secrets_path))

    keyfile(KEY_A)
    secrets_store.save({"SLACK_BOT_TOKEN": "xoxb-old"}, secrets_path)
    keyfile(KEY_B, KEY_A)
    values = secrets_store.load(secrets_path)
    secrets_store.save(values, secrets_path)

    keyfile(KEY_B)                 # step 4: old key gone
    assert secrets_store.load(secrets_path) == {"SLACK_BOT_TOKEN": "xoxb-old"}
