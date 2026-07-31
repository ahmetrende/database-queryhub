"""`create_local_user.py --if-absent`, which is what makes the container's
first-admin bootstrap safe to run on every start.

The default behaviour of the script is to RESET the password of an account that
already exists — deliberate, it is how an operator recovers a locked-out user.
Run unconditionally from the entrypoint, that same behaviour undoes the
operator's own password change on the next restart, and restarts are routine
(`docker compose restart`, a host reboot, a crash loop). Worse, it would restore
the bootstrap password that was printed into the container logs.

So the flag has to short-circuit BEFORE reading the password: an unattended
caller should not have to supply one just to be told there is nothing to do.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import create_local_user as cli  # noqa: E402


@pytest.fixture
def existing(monkeypatch):
    """An account that is already there, and a hard failure on any write."""
    monkeypatch.setattr(cli.local_users, "exists", lambda u: True)
    monkeypatch.setattr(cli.local_users, "normalize_username", lambda u: u)
    monkeypatch.setattr(cli.local_users, "to_identity", lambda u: f"local:{u}")

    def no_writes():
        raise AssertionError("opened a transaction for an existing account")

    monkeypatch.setattr(cli.db, "transaction", lambda *a, **k: no_writes())

    def no_password(*a, **k):
        raise AssertionError("read a password for an account it will not touch")

    monkeypatch.setattr(cli, "_read_password", no_password)


def test_if_absent_is_a_noop_for_an_existing_account(existing, capsys):
    assert cli.main(["--username", "admin", "--admin", "--if-absent",
                     "--password-stdin"]) == 0
    out = capsys.readouterr().out
    assert "already exists" in out and "left untouched" in out


def test_the_message_names_the_account(existing, capsys):
    """The entrypoint prints this straight to the container log, where it is the
    only evidence that a restart did not reset anything."""
    cli.main(["--username", "ci-admin", "--if-absent", "--password-stdin"])
    assert "'ci-admin'" in capsys.readouterr().out


def test_without_the_flag_an_existing_account_is_still_reset(monkeypatch):
    """Guard the guard: if --if-absent silently became the default, the recovery
    path an operator relies on would be gone, and this file would still pass."""
    monkeypatch.setattr(cli.local_users, "exists", lambda u: True)
    monkeypatch.setattr(cli.local_users, "normalize_username", lambda u: u)
    monkeypatch.setattr(cli.local_users, "to_identity", lambda u: f"local:{u}")
    reached = []
    monkeypatch.setattr(cli, "_read_password",
                        lambda **k: reached.append(True) or "x" * 12)
    monkeypatch.setattr(cli.passwords, "hash_password", lambda p: "hash")

    class Boom(Exception):
        pass

    def transaction(*a, **k):
        raise Boom

    monkeypatch.setattr(cli.db, "transaction", transaction)
    with pytest.raises(Boom):
        cli.main(["--username", "admin", "--password-stdin"])
    assert reached, "the password was not read, so the reset path was skipped"
