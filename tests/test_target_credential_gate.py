"""Enabling a target that cannot run anything must fail loudly.

Onboarding a discovered endpoint has two steps and only one of them is visible.
The inventory import writes a sentinel password and leaves the real one to a
human; enabling is a boolean anyone remembers, so the credential is what gets
skipped. Nothing then complains: the target joins the picker, a grant can be
issued on it, the tier badge renders, and the schema snapshot fails quietly so
the tree shows a database with no tables. On 2026-08-06 that combination reached
a user, who reported it from the outside — the system never did.

So the sentinel is refused at `set_enabled`, the single choke point, rather than
warned about somewhere downstream where the enable path could route around it.
"""
import pytest

from queryhub import targets


class _FakeCur:
    def __init__(self, rows): self._rows, self.executed = rows, []
    def execute(self, sql, params=None): self.executed.append((sql, params))
    def fetchall(self): return self._rows


def test_enabling_a_placeholder_target_raises(monkeypatch):
    monkeypatch.setattr(targets, "_is_placeholder", lambda _c: True)
    monkeypatch.setattr(targets.db, "fetch_one",
                        lambda *a, **k: {"alias": "svc-prod-new",
                                         "password_encrypted": "x"})
    wrote = []
    monkeypatch.setattr(targets.db, "execute", lambda *a, **k: wrote.append(a))

    with pytest.raises(targets.CredentialNotProvisioned) as e:
        targets.set_enabled(42, True)

    assert "svc-prod-new" in str(e.value), "the message must name the target"
    assert not wrote, "the UPDATE ran anyway — the guard is decorative"


def test_the_message_says_what_to_do_about_it(monkeypatch):
    """A refusal that does not say how to proceed just gets forced."""
    monkeypatch.setattr(targets, "_is_placeholder", lambda _c: True)
    monkeypatch.setattr(targets.db, "fetch_one",
                        lambda *a, **k: {"alias": "a", "password_encrypted": "x"})
    monkeypatch.setattr(targets.db, "execute", lambda *a, **k: None)
    with pytest.raises(targets.CredentialNotProvisioned) as e:
        targets.set_enabled(1, True)
    msg = str(e.value)
    assert "adopt_target_credential" in msg
    assert "force" in msg


def test_a_provisioned_target_enables_normally(monkeypatch):
    monkeypatch.setattr(targets, "_is_placeholder", lambda _c: False)
    monkeypatch.setattr(targets.db, "fetch_one",
                        lambda *a, **k: {"alias": "ok", "password_encrypted": "real"})
    wrote = []
    monkeypatch.setattr(targets.db, "execute",
                        lambda sql, params=None: wrote.append((sql, params)))
    targets.set_enabled(7, True)
    assert wrote and wrote[0][1] == (True, 7)


def test_force_enables_a_placeholder_on_purpose(monkeypatch):
    """Staging a target before its credential arrives is legitimate — but it has
    to be asked for, so nobody reaches this state by forgetting."""
    monkeypatch.setattr(targets, "_is_placeholder", lambda _c: True)
    called = []
    monkeypatch.setattr(targets.db, "fetch_one",
                        lambda *a, **k: called.append("looked") or {"alias": "a",
                                                                    "password_encrypted": "x"})
    wrote = []
    monkeypatch.setattr(targets.db, "execute",
                        lambda sql, params=None: wrote.append((sql, params)))
    targets.set_enabled(9, True, force=True)
    assert wrote, "force did not write"
    assert not called, "force still paid for the lookup it does not need"


def test_disabling_is_never_blocked(monkeypatch):
    """Turning a target OFF must always work — a broken target is exactly the
    one an operator needs to be able to hide, and checking its credential first
    would be a gate on the wrong direction."""
    monkeypatch.setattr(targets, "_is_placeholder",
                        lambda _c: pytest.fail("disable consulted the credential"))
    wrote = []
    monkeypatch.setattr(targets.db, "execute",
                        lambda sql, params=None: wrote.append((sql, params)))
    targets.set_enabled(9, False)
    assert wrote and wrote[0][1] == (False, 9)


def test_unprovisioned_enabled_reports_only_the_broken_ones(monkeypatch):
    rows = [{"id": 1, "alias": "good", "password_encrypted": "real"},
            {"id": 2, "alias": "bad", "password_encrypted": "sentinel"},
            {"id": 3, "alias": "also-bad", "password_encrypted": "sentinel"}]
    monkeypatch.setattr(targets.db, "fetch_all", lambda *a, **k: rows)
    monkeypatch.setattr(targets, "_is_placeholder",
                        lambda c: c == "sentinel")
    out = targets.unprovisioned_enabled()
    assert [r["alias"] for r in out] == ["bad", "also-bad"]
    assert all("password_encrypted" not in r for r in out), (
        "the health report carries ciphertext it has no reason to expose")
