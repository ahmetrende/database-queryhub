"""Pure tests for auth_events.build_notifications — the outbox-row →
DM-text mapping. No DB: alias/team lookups are injected."""
from queryhub import auth_events


ALIAS = {52: "alpha-prod", 7: "beta-prod"}


def alias_of(tid):
    return ALIAS.get(tid)


def team_info(team_id):
    return ("payments", ["U01AAAAAAAA", "U01BBBBBBBB"]) if team_id == 3 else (None, [])


def ev(table, op, old=None, new=None, user="U0TESTUSER1", team=None):
    return {
        "table_name": table, "op": op, "old_row": old, "new_row": new,
        "slack_user_id": user, "team_id": team,
    }


# --- user_target_grants -----------------------------------------------------

def test_user_grant_insert_dm():
    notes = auth_events.build_notifications(
        ev("user_target_grants", "INSERT", new={
            "slack_user_id": "U0TESTUSER1", "target_server_id": 52,
            "mode": "rw", "allowed_databases": ["nova"],
            "granted_by": "U0GRANTER11", "revoked_at": None}),
        alias_of=alias_of, team_info=team_info)
    assert len(notes) == 1
    uid, text = notes[0]
    assert uid == "U0TESTUSER1"
    assert "*RW*" in text and "`alpha-prod`" in text and "`nova`" in text
    assert "<@U0GRANTER11>" in text


def test_user_grant_born_revoked_is_silent():
    notes = auth_events.build_notifications(
        ev("user_target_grants", "INSERT", new={
            "target_server_id": 52, "mode": "ro",
            "revoked_at": "2026-01-01T00:00:00"}),
        alias_of=alias_of, team_info=team_info)
    assert notes == []


def test_user_grant_revoke_transition_dm():
    notes = auth_events.build_notifications(
        ev("user_target_grants", "UPDATE",
           old={"target_server_id": 52, "mode": "ro", "revoked_at": None},
           new={"target_server_id": 52, "mode": "ro",
                "revoked_at": "2026-07-14T00:00:00"}),
        alias_of=alias_of, team_info=team_info)
    assert len(notes) == 1
    assert "revoked" in notes[0][1]


def test_user_grant_restore_transition_dm():
    notes = auth_events.build_notifications(
        ev("user_target_grants", "UPDATE",
           old={"target_server_id": 52, "mode": "ro",
                "revoked_at": "2026-07-01T00:00:00"},
           new={"target_server_id": 52, "mode": "rw", "revoked_at": None,
                "allowed_databases": None}),
        alias_of=alias_of, team_info=team_info)
    assert len(notes) == 1
    assert "restored" in notes[0][1] and "*RW*" in notes[0][1]


def test_user_grant_mode_change_dm():
    notes = auth_events.build_notifications(
        ev("user_target_grants", "UPDATE",
           old={"target_server_id": 52, "mode": "ro", "revoked_at": None},
           new={"target_server_id": 52, "mode": "rw", "revoked_at": None}),
        alias_of=alias_of, team_info=team_info)
    assert len(notes) == 1
    assert "*RW*" in notes[0][1]


def test_user_grant_uninteresting_update_is_silent():
    notes = auth_events.build_notifications(
        ev("user_target_grants", "UPDATE",
           old={"target_server_id": 52, "mode": "ro", "revoked_at": None,
                "granted_by": "A"},
           new={"target_server_id": 52, "mode": "ro", "revoked_at": None,
                "granted_by": "B"}),
        alias_of=alias_of, team_info=team_info)
    assert notes == []


# --- auto_approve_grants ----------------------------------------------------

def test_auto_approve_insert_permanent():
    notes = auth_events.build_notifications(
        ev("auto_approve_grants", "INSERT", new={
            "max_tier": "ro", "target_server_id": 52,
            "database_name": "nova", "expires_at": None,
            "granted_by": "U0GRANTER11"}),
        alias_of=alias_of, team_info=team_info)
    assert len(notes) == 1
    text = notes[0][1]
    assert "Auto-approve active" in text
    assert "*permanent*" in text
    assert "`alpha-prod`" in text and "`nova`" in text


def test_auto_approve_insert_window():
    notes = auth_events.build_notifications(
        ev("auto_approve_grants", "INSERT", new={
            "max_tier": "ro", "target_server_id": 52, "database_name": None,
            "expires_at": "2026-07-14T18:00:00+00:00"}),
        alias_of=alias_of, team_info=team_info)
    assert "until `2026-07-14 18:00 UTC`" in notes[0][1]
    assert "all databases" in notes[0][1]


def test_auto_approve_delete_dm():
    notes = auth_events.build_notifications(
        ev("auto_approve_grants", "DELETE", old={
            "max_tier": "ro", "target_server_id": 52, "database_name": "nova"}),
        alias_of=alias_of, team_info=team_info)
    assert "removed" in notes[0][1]


# --- requesters / admins ----------------------------------------------------

def test_requester_disable_dm():
    notes = auth_events.build_notifications(
        ev("requesters", "UPDATE",
           old={"enabled": True}, new={"enabled": False}),
        alias_of=alias_of, team_info=team_info)
    assert "disabled" in notes[0][1]


def test_requester_insert_enabled_dm():
    notes = auth_events.build_notifications(
        ev("requesters", "INSERT", new={"enabled": True}),
        alias_of=alias_of, team_info=team_info)
    assert "whitelisted" in notes[0][1]


def test_admin_can_grant_flip_dm():
    notes = auth_events.build_notifications(
        ev("admins", "UPDATE",
           old={"enabled": True, "can_grant": False},
           new={"enabled": True, "can_grant": True}),
        alias_of=alias_of, team_info=team_info)
    assert "can now grant access" in notes[0][1]


def test_temp_admin_insert_dm():
    notes = auth_events.build_notifications(
        ev("temp_admin_grants", "INSERT", new={
            "max_tier": "rw", "expires_at": "2026-08-01T09:00:00",
            "granted_by": "U0GRANTER11"}),
        alias_of=alias_of, team_info=team_info)
    assert "Temporary *admin*" in notes[0][1]
    assert "until `2026-08-01 09:00 UTC`" in notes[0][1]


# --- row-limit overrides ----------------------------------------------------

def test_row_limit_insert_dm_formats_thousands():
    notes = auth_events.build_notifications(
        ev("user_row_limit_overrides", "INSERT", new={
            "max_rows": 500000, "expires_at": None,
            "granted_by": "U0GRANTER11"}),
        alias_of=alias_of, team_info=team_info)
    assert "*500,000*" in notes[0][1]


def test_row_limit_delete_dm():
    notes = auth_events.build_notifications(
        ev("user_row_limit_overrides", "DELETE", old={"max_rows": 500000}),
        alias_of=alias_of, team_info=team_info)
    assert "normal limit" in notes[0][1]


# --- team-scoped fan-out ----------------------------------------------------

def test_team_grant_insert_fans_out_to_members():
    notes = auth_events.build_notifications(
        ev("team_target_grants", "INSERT", user=None, team=3, new={
            "team_id": 3, "target_server_id": 7, "mode": "ro",
            "allowed_databases": None}),
        alias_of=alias_of, team_info=team_info)
    assert {u for u, _ in notes} == {"U01AAAAAAAA", "U01BBBBBBBB"}
    assert all("`payments`" in t and "`beta-prod`" in t for _, t in notes)


def test_team_member_add_dm():
    notes = auth_events.build_notifications(
        ev("team_members", "INSERT", team=3, new={
            "team_id": 3, "slack_user_id": "U0TESTUSER1"}),
        alias_of=alias_of, team_info=team_info)
    assert "added to team `payments`" in notes[0][1]


def test_unknown_table_is_silent():
    notes = auth_events.build_notifications(
        ev("some_other_table", "INSERT", new={}),
        alias_of=alias_of, team_info=team_info)
    assert notes == []


# --- one admin action, one message ------------------------------------------
#
# The trigger fires per ROW, which is right: granting read access across the
# thirteen servers a person can reach is one decision to the admin and thirteen
# rows in the table. It was also thirteen separate DMs. Coalescing belongs at
# the point where the recipient is known, not in the trigger.

class _Client:
    """Records DMs. `fail_for` raises for those recipients."""
    def __init__(self, fail_for=()):
        self.sent = []                      # (uid, text)
        self.fail_for = set(fail_for)

    def chat_postMessage(self, **kw):       # not used; dm_requester is patched
        raise AssertionError("unexpected direct API call")


class _Cur:
    def __init__(self, events):
        self._events = events
        self.updates = []                   # (sql_fragment, id)

    def execute(self, sql, params=()):
        if "SELECT" in sql and "auth_event_outbox" in sql:
            self._rows = self._events
        else:
            frag = "processed" if "processed_at = NOW()" in sql else "failed"
            self.updates.append((frag, params[-1]))
            self._rows = []

    def fetchall(self):
        return self._rows


def _ev(i, uid, target):
    return {"id": i, "table_name": "auto_approve_grants", "op": "INSERT",
            "slack_user_id": uid, "team_id": None, "attempts": 0,
            "old_row": None,
            "new_row": {"max_tier": "ro", "target_server_id": target,
                        "database_name": None, "expires_at": None,
                        "granted_by": "U0ADMIN123"}}


def test_a_single_change_keeps_its_exact_wording():
    """The common case must not gain a wrapper — this is the message people
    already recognise."""
    from queryhub.auth_events import combine_notes
    assert combine_notes([":zap: Auto-approve active"]) == ":zap: Auto-approve active"


def test_several_changes_become_one_bulleted_message():
    from queryhub.auth_events import combine_notes
    out = combine_notes(["first", "second", "third"])
    assert out.startswith("*3 access changes*")
    assert out.count("•") == 3
    assert "first" in out and "third" in out


def test_thirteen_rows_send_one_dm(monkeypatch):
    """The regression: thirteen grants for one person used to be thirteen DMs."""
    from queryhub import auth_events as ae
    sent = []
    monkeypatch.setattr(ae, "_alias_of", lambda tid: f"conn-{tid}")
    monkeypatch.setattr(ae, "_team_info", lambda tid: (None, []))
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_requester",
                        lambda c, uid, text: sent.append((uid, text)))
    events = [_ev(i, "U0AB12CD34", i) for i in range(1, 14)]
    cur = _Cur(events)
    monkeypatch.setattr(ae.db, "transaction",
                        lambda: __import__("contextlib").nullcontext(cur))

    handled = ae.process_pending(object())

    assert len(sent) == 1, f"expected one DM, got {len(sent)}"
    assert sent[0][0] == "U0AB12CD34"
    assert sent[0][1].startswith("*13 access changes*")
    assert handled == 13
    assert sorted(i for f, i in cur.updates if f == "processed") == list(range(1, 14))


def test_two_people_get_one_dm_each(monkeypatch):
    from queryhub import auth_events as ae
    sent = []
    monkeypatch.setattr(ae, "_alias_of", lambda tid: f"conn-{tid}")
    monkeypatch.setattr(ae, "_team_info", lambda tid: (None, []))
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_requester",
                        lambda c, uid, text: sent.append((uid, text)))
    events = [_ev(1, "U0AB12CD34", 1), _ev(2, "U0AB12CD34", 2),
              _ev(3, "U0XY98ZW76", 3)]
    cur = _Cur(events)
    monkeypatch.setattr(ae.db, "transaction",
                        lambda: __import__("contextlib").nullcontext(cur))

    ae.process_pending(object())

    assert sorted(uid for uid, _ in sent) == ["U0AB12CD34", "U0XY98ZW76"]
    both = dict(sent)
    assert both["U0AB12CD34"].startswith("*2 access changes*")
    assert not both["U0XY98ZW76"].startswith("*")     # single → verbatim


def test_a_failed_send_leaves_only_its_own_events_unprocessed(monkeypatch):
    """One unreachable recipient must not hold back everyone else's."""
    from queryhub import auth_events as ae
    monkeypatch.setattr(ae, "_alias_of", lambda tid: f"conn-{tid}")
    monkeypatch.setattr(ae, "_team_info", lambda tid: (None, []))

    def dm(c, uid, text):
        if uid == "U0BAD00000":
            raise RuntimeError("channel_not_found")
    from queryhub.slack_app import notifications
    monkeypatch.setattr(notifications, "dm_requester", dm)

    events = [_ev(1, "U0AB12CD34", 1), _ev(2, "U0BAD00000", 2)]
    cur = _Cur(events)
    monkeypatch.setattr(ae.db, "transaction",
                        lambda: __import__("contextlib").nullcontext(cur))

    handled = ae.process_pending(object())

    assert handled == 1
    assert ("processed", 1) in cur.updates
    assert ("failed", 2) in cur.updates
