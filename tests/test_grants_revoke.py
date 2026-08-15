"""grants.revoke and the two grantee notifications — the untested half.

tests/test_grants_authz.py covers who may hand access out. Taking it away was
uncovered end to end: revoke(), notify_grantee(), notify_grantee_revoked(), and
the two listing helpers accounted for most of the 41% of grants.py that no test
executed, while the module sat behind CI's "access-deciding" coverage gate. That
gate passed anyway, because it measured the AGGREGATE over five files and
query_safety.py's 642 well-covered statements hid grants.py's 43 uncovered ones.
Revocation is half of an access-control system; it deserves better than a number
that averages it away.

The properties here are the ones that would matter at 3am:

  * revoke() is idempotent — no active grant means None, not an exception and
    not a spurious audit row. Two admins clicking the same button must not
    produce two revocations.
  * every revoke writes an audit_log row, in the SAME transaction as the
    UPDATE, so a forensic trail cannot silently diverge from reality.
  * the transaction sets app.auth_dm_suppress, because this path DMs the user
    itself; without it the mig-060 outbox poller sends a second DM.
  * both notify helpers swallow everything. Their docstrings promise a
    notification failure "must not undo a committed grant" — and since the DM
    happens after the COMMIT, a raised exception there would propagate to the
    caller as if the revoke had failed when it had not.
"""
import pytest

from dba_slack_bot import grants


class FakeCursor:
    """Records every statement, answers fetchone() from a queue."""

    def __init__(self, results):
        self.results = list(results)
        self.statements: list[tuple[str, tuple | None]] = []
        self._last = None

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.statements.append((flat, params))
        # Only statements that actually return rows consume a queued result —
        # otherwise the `SET LOCAL` eats the row meant for the UPDATE, and the
        # test ends up asserting against the wrong statement.
        if "RETURNING" in flat.upper() or flat.upper().startswith("SELECT"):
            self._last = self.results.pop(0) if self.results else None

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._last or []

    def sql_containing(self, needle):
        return [s for s, _ in self.statements if needle.lower() in s.lower()]


@pytest.fixture
def cursor(monkeypatch):
    """Swap db.transaction for a recording cursor. The box lets a test set the
    fetchone() results before calling in."""
    import contextlib
    box = {"cursor": None, "results": []}

    @contextlib.contextmanager
    def fake_transaction():
        box["cursor"] = FakeCursor(box["results"])
        yield box["cursor"]

    monkeypatch.setattr(grants.db, "transaction", fake_transaction)
    # Never send a real DM, and never look up a real target.
    monkeypatch.setattr(grants, "notify_grantee_revoked",
                        lambda *a, **k: box.setdefault("dm", (a, k)))
    return box


# --------------------------------------------------------------- revoke


def test_revoking_a_grant_that_is_not_there_returns_none(cursor):
    """No active row -> None. The caller distinguishes "nothing to do" from
    "done", and nothing else in the transaction should have happened."""
    cursor["results"] = [None]          # the UPDATE ... RETURNING finds nothing
    out = grants.revoke(granter_id="U0DBA", granter_name="DBA",
                        grantee_id="U0DEV", target_id=7)
    assert out is None
    # No audit row for a revoke that did not occur.
    assert cursor["cursor"].sql_containing("INSERT INTO audit_log") == []
    assert "dm" not in cursor, "no DM for a no-op revoke"


def test_revoke_returns_the_row_and_audits_it(cursor):
    row = {"slack_user_id": "U0DEV", "target_server_id": 7, "mode": "rw"}
    cursor["results"] = [row]
    out = grants.revoke(granter_id="U0DBA", granter_name="DBA",
                        grantee_id="U0DEV", target_id=7, reason="left the team",
                        notify=False)
    assert out == row

    audits = cursor["cursor"].sql_containing("INSERT INTO audit_log")
    assert len(audits) == 1, "exactly one audit row per revoke"
    # The action name is what an auditor greps for.
    assert "access_revoked" in audits[0]
    # The reason and the tier that was taken away both have to be recoverable.
    details = cursor["cursor"].statements[-1][1]
    assert "left the team" in str(details)
    assert "rw" in str(details)


def test_revoke_only_touches_the_named_user_target_and_only_if_active(cursor):
    """The UPDATE must be scoped three ways. A missing `revoked_at IS NULL`
    would re-revoke an old grant and move its timestamp, rewriting history."""
    cursor["results"] = [{"slack_user_id": "U0DEV", "target_server_id": 7,
                          "mode": "ro"}]
    grants.revoke(granter_id="U0DBA", granter_name=None,
                  grantee_id="U0DEV", target_id=7, notify=False)
    update = cursor["cursor"].sql_containing("UPDATE user_target_grants")[0]
    assert "slack_user_id = %s" in update
    assert "target_server_id = %s" in update
    assert "revoked_at IS NULL" in update


def test_revoke_suppresses_the_outbox_dm(cursor):
    """mig 060 appends to auth_event_outbox on any grant change and a poller
    DMs the user. This path sends its own DM, so without SET LOCAL
    app.auth_dm_suppress the user gets told twice."""
    cursor["results"] = [{"slack_user_id": "U0DEV", "target_server_id": 7,
                          "mode": "ro"}]
    grants.revoke(granter_id="U0DBA", granter_name=None,
                  grantee_id="U0DEV", target_id=7, notify=False)
    suppress = cursor["cursor"].sql_containing("auth_dm_suppress")
    assert suppress, "the outbox DM was not suppressed — user gets two DMs"
    assert "SET LOCAL" in suppress[0], "must be SET LOCAL, not session-wide"
    # ...and it has to come before the UPDATE that fires the trigger.
    order = [i for i, (s, _) in enumerate(cursor["cursor"].statements)
             if "auth_dm_suppress" in s or "UPDATE user_target_grants" in s]
    first = cursor["cursor"].statements[order[0]][0]
    assert "auth_dm_suppress" in first


def test_revoke_notifies_by_default(cursor, monkeypatch):
    """Symmetric with grant(): every revoke path tells the user, so an admin
    running a one-off script cannot silently strip access."""
    cursor["results"] = [{"slack_user_id": "U0DEV", "target_server_id": 7,
                          "mode": "ddl"}]
    seen = {}
    monkeypatch.setattr(grants, "notify_grantee_revoked",
                        lambda *a, **k: seen.setdefault("args", a))
    monkeypatch.setattr("dba_slack_bot.targets.get",
                        lambda tid: type("T", (), {"alias": "demo-primary"})())
    grants.revoke(granter_id="U0DBA", granter_name=None,
                  grantee_id="U0DEV", target_id=7)
    assert seen["args"][0] == "U0DEV"
    assert "demo-primary" in seen["args"]
    assert "ddl" in seen["args"]


def test_revoke_notification_survives_an_unknown_target(cursor, monkeypatch):
    """targets.get() returning None must not crash the notification — the
    revoke is already committed at that point."""
    cursor["results"] = [{"slack_user_id": "U0DEV", "target_server_id": 99,
                          "mode": "ro"}]
    seen = {}
    monkeypatch.setattr(grants, "notify_grantee_revoked",
                        lambda *a, **k: seen.setdefault("args", a))
    monkeypatch.setattr("dba_slack_bot.targets.get", lambda tid: None)
    assert grants.revoke(granter_id="U0DBA", granter_name=None,
                         grantee_id="U0DEV", target_id=99) is not None
    assert "99" in seen["args"], "falls back to the numeric id"


# ------------------------------------------------- the notification contract


@pytest.mark.parametrize("fn,args", [
    (grants.notify_grantee, ("U0DEV", "U0DBA", ["demo-primary"], "ro", ["appdb"])),
    (grants.notify_grantee_revoked, ("U0DEV", "U0DBA", "demo-primary", "ro")),
])
def test_notifications_never_raise(fn, args, monkeypatch):
    """Both docstrings promise it, and the promise carries weight: the DM is
    sent AFTER the transaction commits, so an exception escaping here reaches
    the caller as a failed grant/revoke that actually succeeded — and an admin
    who sees an error will try again.

    Slack down is the realistic case, so break it at the client.
    """
    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("slack is unreachable")

    monkeypatch.setattr("slack_sdk.web.WebClient", Boom)
    fn(*args)   # must return normally


def test_notification_reaches_the_grantee_with_the_tier_and_scope(monkeypatch):
    """The happy path, because the DM's content is the user's only signal of
    what they can now do."""
    posted = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def conversations_open(self, users):
            posted["opened_for"] = users
            return {"channel": {"id": "D123"}}

        def chat_postMessage(self, channel, text):
            posted["channel"], posted["text"] = channel, text

    monkeypatch.setattr("slack_sdk.web.WebClient", FakeClient)
    grants.notify_grantee("U0DEV", "U0DBA", ["demo-primary", "demo-replica"],
                          "rw", ["appdb"], whitelisted=True)

    assert posted["opened_for"] == "U0DEV"
    assert posted["channel"] == "D123"
    text = posted["text"]
    assert "RW" in text, "the tier must be explicit"
    assert "demo-primary" in text and "demo-replica" in text
    assert "appdb" in text, "the database scope must be explicit"
    assert "<@U0DBA>" in text, "who granted it"
    assert "set up in QueryHub" in text, "first-time users get the extra line"


def test_notification_says_all_databases_when_unscoped(monkeypatch):
    """`databases=None` means every database on the target. Rendering that as
    an empty scope would understate the access just handed over."""
    posted = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def conversations_open(self, users):
            return {"channel": {"id": "D1"}}

        def chat_postMessage(self, channel, text):
            posted["text"] = text

    monkeypatch.setattr("slack_sdk.web.WebClient", FakeClient)
    grants.notify_grantee("U0DEV", None, ["demo-primary"], "ro", None)
    assert "all databases" in posted["text"]
    assert "by <@" not in posted["text"], "no granter -> no 'by' clause"
