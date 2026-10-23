"""A masking exemption with an end, for a chosen audience (PLA-1592 U15).

Two new dimensions on `pii_masking_exemptions` (migration 149):

  * `expiresAt` -- after it the row is not applied, so masking comes back;
  * `audience` -- {kind: everyone|super|dba|team, teamId?}. `dba` is a holder
    of any admin role (`admins.is_admin`, which is `access.is_admin` under
    access_model_v2), super-admins included.

Every reader goes through `pii._load_exemptions`: the result path (executor),
the header dots on a delivered result (routes_queries), and the preview's
"today" side. These tests drive that loader with the rows the database would
return and readers of every kind. The SQL half of it -- the end filter -- is
proved against PostgreSQL in tests/test_integration_expiry_rules.py.

Every lookup about the reader fails CLOSED: an error is "not in the audience".
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from queryhub import admins, auth_events, pii
from queryhub import db as db_mod
from queryhub.web import routes_admin as ra
from queryhub.web import routes_queries

AHEAD = datetime.now(timezone.utc) + timedelta(days=5)


def _row(**kw):
    base = {"database_name": "app", "schema_name": None, "table_name": None,
            "column_name": "email", "apply_in_joins": False,
            "keep_value_scan": False, "super_admin_only": False,
            "audience": "everyone", "audience_team_id": None}
    base.update(kw)
    return base


@pytest.fixture
def readers(monkeypatch):
    """Super, admin and team membership by principal id, and a log of who was
    looked up. The rows come from `st["rows"]`, as the database returns them."""
    st = {"rows": [], "asked": [], "teams": {"U0MEMBER01": {14}},
          "supers": {"U0SUPER001"}, "admins": {"U0SUPER001", "U0ADMIN001"}}
    monkeypatch.setattr(db_mod, "fetch_all", lambda *a, **k: [dict(r) for r in st["rows"]])
    monkeypatch.setattr(admins, "is_super_admin",
                        lambda uid: st["asked"].append(("super", uid)) or uid in st["supers"])
    monkeypatch.setattr(admins, "is_admin",
                        lambda uid: st["asked"].append(("admin", uid)) or uid in st["admins"])
    monkeypatch.setattr(pii, "_reader_team_ids",
                        lambda uid: st["asked"].append(("team", uid)) or st["teams"].get(uid, set()))
    return st


def _sees(st, reader):
    return [r["column_name"] for r in pii._load_exemptions(7, "app", reader)]


# --- the loader: who it applies to ------------------------------------------

def test_everyone_rows_need_no_lookup(readers):
    readers["rows"] = [_row(column_name="email"), _row(column_name="phone")]
    assert _sees(readers, "U0PLAIN001") == ["email", "phone"]
    assert readers["asked"] == []


@pytest.mark.parametrize("reader,expected", [
    ("U0ADMIN001", ["dba_col"]), ("U0SUPER001", ["dba_col"]),
    ("U0PLAIN001", []), (None, [])])
def test_a_dba_row_applies_to_admin_role_holders_only(readers, reader, expected):
    readers["rows"] = [_row(column_name="dba_col", audience="dba")]
    assert _sees(readers, reader) == expected


@pytest.mark.parametrize("reader,expected", [
    ("U0SUPER001", ["s"]), ("U0ADMIN001", []), ("U0PLAIN001", [])])
def test_a_super_row_applies_to_super_admins_only(readers, reader, expected):
    readers["rows"] = [_row(column_name="s", audience="super", super_admin_only=True)]
    assert _sees(readers, reader) == expected


@pytest.mark.parametrize("audience", ["everyone", "dba"])
def test_the_old_flag_alone_narrows_a_row_to_super_admins(readers, audience):
    """A psql habit: set super_admin_only and leave the new column alone. The
    row narrows. It never widens."""
    readers["rows"] = [_row(column_name="f", audience=audience, super_admin_only=True)]
    assert _sees(readers, "U0ADMIN001") == []
    assert _sees(readers, "U0SUPER001") == ["f"]


@pytest.mark.parametrize("reader,expected", [
    ("U0MEMBER01", ["t"]), ("U0PLAIN001", []), ("U0SUPER001", []), (None, [])])
def test_a_team_row_applies_to_that_teams_members_only(readers, reader, expected):
    readers["rows"] = [_row(column_name="t", audience="team", audience_team_id=14)]
    assert _sees(readers, reader) == expected


def test_a_team_row_with_the_old_flag_needs_both(readers):
    readers["rows"] = [_row(column_name="t", audience="team", audience_team_id=14,
                            super_admin_only=True)]
    assert _sees(readers, "U0MEMBER01") == []
    readers["supers"].add("U0MEMBER01")
    assert _sees(readers, "U0MEMBER01") == ["t"]


def test_a_value_this_code_cannot_read_applies_to_nobody(readers):
    readers["rows"] = [_row(column_name="x", audience="contractors"),
                       _row(column_name="y", audience="team", audience_team_id=None),
                       _row(column_name="z", audience="contractors", audience_team_id=14)]
    assert _sees(readers, "U0SUPER001") == []
    assert _sees(readers, "U0MEMBER01") == []


def test_each_question_is_asked_once_per_result(readers):
    readers["rows"] = [_row(column_name=c, audience="dba") for c in ("a", "b", "c")]
    _sees(readers, "U0ADMIN001")
    assert readers["asked"] == [("admin", "U0ADMIN001")]


@pytest.mark.parametrize("who,still", [("is_admin", ["s"]), ("is_super_admin", ["a"])])
def test_an_unavailable_role_lookup_keeps_masking_on(readers, monkeypatch, who, still):
    """Only the row whose question failed stays masked; the other question
    still has its answer."""
    def boom(_uid):
        raise RuntimeError("metadata DB down")
    monkeypatch.setattr(admins, who, boom)
    readers["rows"] = [_row(column_name="a", audience="dba"),
                       _row(column_name="s", audience="super")]
    assert _sees(readers, "U0SUPER001") == still


def test_an_unavailable_team_lookup_keeps_masking_on(readers, monkeypatch):
    def boom(_uid):
        raise RuntimeError("metadata DB down")
    monkeypatch.setattr(pii, "_reader_team_ids", boom)
    readers["rows"] = [_row(column_name="t", audience="team", audience_team_id=14)]
    assert _sees(readers, "U0MEMBER01") == []


def test_a_team_audience_needs_the_new_access_model(monkeypatch):
    """The id names a `team` row, which only access_model_v2 reads."""
    from queryhub import teams
    monkeypatch.setattr(teams, "use_v2", lambda: False)
    monkeypatch.setattr(db_mod, "fetch_all",
                        lambda *a, **k: pytest.fail("read a team the switch does not use"))
    assert pii._reader_team_ids("U0MEMBER01") == set()


def test_a_reader_is_only_a_member_of_live_teams():
    src = inspect.getsource(pii._reader_team_ids)
    assert "NOT t.is_deleted" in src and "NOT tm.is_deleted" in src


def test_an_ended_row_is_not_read():
    """Filtered by the database in the same statement, never by a sweep."""
    src = inspect.getsource(pii._load_exemptions)
    assert "AND (expires_at IS NULL OR expires_at > NOW())" in src


def test_the_result_path_decides_with_the_loaders_answer(readers):
    """exemption_decision on a column the masker would catch by name."""
    readers["rows"] = [_row(column_name="email", audience="dba")]
    sql, cols = "SELECT email FROM users", ["email"]
    assert pii.exemption_decision(7, "app", sql, cols, principal_id="U0ADMIN001") \
        == (False, {0})
    assert pii.exemption_decision(7, "app", sql, cols, principal_id="U0PLAIN001") \
        == (False, set())


def test_the_header_dots_ask_the_same_loader_for_the_requester():
    src = inspect.getsource(routes_queries._masked_pii_cols)
    assert 'principal_id=row["requester_slack_id"]' in src


def test_the_preview_reads_today_as_an_ordinary_reader():
    assert "pii._load_exemptions(tid, database, None)" in inspect.getsource(
        ra._mask_preview_pair)


# --- what the admin API writes ----------------------------------------------

@pytest.fixture
def writes(monkeypatch):
    st = {"v2": True, "teams": {14: {"id": 14, "name": "team-a", "display_name": "Team A"}}}
    monkeypatch.setattr(ra, "_target_id_of", lambda alias: 7 if alias == "prod-main" else None)
    monkeypatch.setattr(ra.teams_mod, "use_v2", lambda: st["v2"])
    monkeypatch.setattr(ra, "_team_for_write", lambda tid: st["teams"].get(tid))
    return st


def _body(**kw):
    base = {"connectionId": "prod-main", "databaseId": "app", "schema": "public",
            "table": "users", "column": "email", "reason": "a wallet address column"}
    base.update(kw)
    return ra.MaskExemptionIn(**base)


def _err(fn, *a):
    with pytest.raises(HTTPException) as e:
        fn(*a)
    return e.value.status_code


@pytest.mark.parametrize("audience,kind,team,flag", [
    ("everyone", "everyone", None, False), ("super", "super", None, True),
    ("dba", "dba", None, False), ({"kind": "dba"}, "dba", None, False),
    ({"kind": "team", "teamId": "14"}, "team", 14, False),
    ({"kind": "team", "teamId": 14}, "team", 14, False)])
def test_both_wire_forms_are_read(writes, audience, kind, team, flag):
    _t, f = ra._mask_fields(_body(audience=audience))
    assert (f["audience"], f["audience_team_id"], f["super_admin_only"]) == (kind, team, flag)


@pytest.mark.parametrize("audience,status", [
    ({"kind": "team"}, 400), ("team", 400),
    ({"kind": "team", "teamId": "team-a"}, 400),
    ({"kind": "dba", "teamId": 14}, 400), ({"kind": "contractors"}, 400),
    ({"kind": "team", "teamId": 99}, 404)])
def test_an_audience_that_names_nobody_is_refused(writes, audience, status):
    assert _err(ra._mask_fields, _body(audience=audience)) == status


def test_a_team_audience_without_the_new_model_is_refused(writes):
    writes["v2"] = False
    assert _err(ra._mask_fields, _body(audience={"kind": "team", "teamId": 14})) == 400


def test_an_end_is_written_when_sent_and_refused_when_past(writes):
    _t, f = ra._mask_fields(_body(expiresAt=AHEAD.isoformat()))
    assert f["expires_at"] == AHEAD
    assert ra._mask_fields(_body())[1]["expires_at"] is None
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert _err(ra._mask_fields, _body(expiresAt=past)) == 400


def test_an_edit_leaves_an_unsent_end_alone_and_clears_a_null_one():
    assert "expires_at" not in ra._mask_edit_fields(ra.MaskPatchIn(reason="checked"))
    assert ra._mask_edit_fields(ra.MaskPatchIn(expiresAt=None)) == {"expires_at": None}
    got = ra._mask_edit_fields(ra.MaskPatchIn(expiresAt=AHEAD.isoformat()))
    assert got == {"expires_at": AHEAD}


def test_an_edit_maps_the_audience_to_both_columns():
    got = ra._mask_edit_fields(ra.MaskPatchIn(audience={"kind": "dba"}))
    assert got == {"super_admin_only": False, "audience": "dba", "audience_team_id": None}


def test_who_and_until_when_are_settings_not_reach():
    """`uq_pii_masking_exemptions_scope` allows one row per reach, so a second
    audience on a reach is an Edit of that row, and the duplicate check stays
    on the reach alone."""
    src = inspect.getsource(ra.admin_add_mask_exemption)
    dupe = src[src.index("dupe = db.fetch_one("):src.index("if dupe:")]
    assert "audience" not in dupe and "expires_at" not in dupe


class _PatchCur:
    def __init__(self, before):
        self.before = before
        self.sql: list[tuple[str, dict]] = []
        self._row = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if sql.lstrip().startswith("SELECT"):
            self._row = dict(self.before)
        else:
            self._row = {**self.before, **{k: v for k, v in (params or {}).items()
                                            if k in self.before}}

    def fetchone(self):
        return self._row


@pytest.fixture
def patching(monkeypatch, writes):
    before = {"id": 31, "target_server_id": 7, "database_name": "app",
              "schema_name": "public", "table_name": "users", "column_name": "email",
              "reason": "r", "enabled": True, "created_by": "U0EXAMPLE001",
              "created_at": None, "updated_by": None, "updated_at": None,
              "apply_in_joins": False, "keep_value_scan": False,
              "super_admin_only": False, "audience": "team", "audience_team_id": 14,
              "expires_at": None, "expired": False}
    st = {"cur": _PatchCur(before), "audit": [], "checked": []}

    class Txn:
        def __enter__(self):
            return st["cur"]

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(ra.admin, "require_admin", lambda claims, need: claims["sub"])
    monkeypatch.setattr(ra.db, "transaction", lambda: Txn())
    monkeypatch.setattr(ra.db, "fetch_all", lambda *a, **k: [])
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda cur, rid, uid, name, action, details=None:
                        st["audit"].append((action, details)))
    monkeypatch.setattr(ra, "_mask_team_check",
                        lambda tid: st["checked"].append(tid))
    return st


def _edit(**kw):
    return ra.admin_set_mask_exemption(31, ra.MaskPatchIn(**kw),
                                       claims={"sub": "U0EXAMPLE001", "name": "Jordan Ray"})


def test_echoing_team_back_without_an_id_changes_nothing(patching):
    """The current screen sends every setting back on save. It knows only the
    string, so a team row comes back as "team" -- with no id."""
    out = _edit(audience="team", reason="r")
    assert out["changed"] is False and patching["audit"] == []
    assert out["audience"] == "team" and out["audienceTeamId"] == "14"


def test_team_without_an_id_on_a_row_with_no_team_is_refused(patching):
    patching["cur"].before.update(audience="everyone", audience_team_id=None)
    with pytest.raises(HTTPException) as e:
        _edit(audience="team")
    assert e.value.status_code == 400


def test_a_new_team_is_checked_and_an_end_is_audited_as_text(patching):
    out = _edit(audience={"kind": "team", "teamId": 15}, expiresAt=AHEAD.isoformat())
    assert patching["checked"] == [15]
    assert out["changed"] is True
    [(action, d)] = patching["audit"]
    assert action == "pii_exemption_changed"
    assert d["changed"]["expires_at"] == {"from": None, "to": AHEAD.isoformat()}
    assert d["changed"]["audience_team_id"] == {"from": 14, "to": 15}


# --- what the admin API reads back ------------------------------------------

def test_a_row_says_who_it_is_for_and_until_when():
    base = {"id": 3, "target_server_id": 7, "database_name": "app",
            "schema_name": None, "table_name": None, "column_name": "email",
            "reason": "r", "enabled": True, "created_by": None, "created_at": None,
            "apply_in_joins": False, "keep_value_scan": False, "alias": "prod-main"}
    row = ra._mask_row_core({**base, "super_admin_only": False, "audience": "team",
                             "audience_team_id": 14, "audience_team_name": "Team A",
                             "expires_at": AHEAD, "expired": False})
    assert (row["audience"], row["audienceTeamId"], row["audienceTeamName"]) == \
        ("team", "14", "Team A")
    assert row["expiresAt"] == AHEAD.isoformat() and row["expired"] is False
    old = ra._mask_row_core({**base, "super_admin_only": True})
    assert old["audience"] == "super" and old["expiresAt"] is None and old["expired"] is False


def test_an_ended_row_is_no_sibling_servers_cover():
    src = inspect.getsource(ra.admin_mask_exemptions)
    assert 'live = [r for r in rows if r["enabled"] and not r.get("expired")]' in src
    assert "(e.expires_at IS NOT NULL AND e.expires_at <= NOW()) AS expired" in src


def test_team_names_are_read_once(monkeypatch):
    asked = []
    monkeypatch.setattr(ra.db, "fetch_all",
                        lambda sql, params=None: asked.append(params) or
                        [{"id": 14, "name": "Team A"}])
    rows = [{"audience_team_id": 14}, {"audience_team_id": None}, {"audience_team_id": 14}]
    ra._mask_team_names(rows)
    assert asked == [([14],)]
    assert [r.get("audience_team_name") for r in rows] == ["Team A", None, "Team A"]


# --- the admins are told -----------------------------------------------------

def _mask_ev(op, old=None, new=None):
    return {"table_name": "pii_masking_exemptions", "op": op, "old_row": old,
            "new_row": new, "slack_user_id": None, "team_id": None}


def test_a_new_row_says_who_and_until_when():
    row = {"enabled": True, "target_server_id": 7, "database_name": "app",
           "table_name": "users", "column_name": "email", "audience": "team",
           "audience_team_id": 14, "expires_at": "2026-10-31T17:00:00+00:00",
           "reason": "r"}
    [(_u, text)] = auth_events.build_notifications(
        _mask_ev("INSERT", new=row), alias_of=lambda t: "prod-main",
        team_info=lambda tid, table=None: ("Team A", []),
        admin_ids=lambda: ["U0EXAMPLE001"])
    assert "for team `Team A` only, until `2026-10-31 17:00 UTC`" in text


@pytest.mark.parametrize("field,old,new", [
    ("audience", "everyone", "dba"), ("audience_team_id", 14, 15),
    ("expires_at", None, "2026-10-31T17:00:00+00:00")])
def test_a_change_of_who_or_until_when_is_a_change(field, old, new):
    base = {"enabled": True, "target_server_id": 7, "column_name": "email",
            "audience": "team", "audience_team_id": 14}
    notes = auth_events.build_notifications(
        _mask_ev("UPDATE", old={**base, field: old}, new={**base, field: new}),
        alias_of=lambda t: "prod-main", team_info=lambda tid, table=None: (None, []),
        admin_ids=lambda: ["U0EXAMPLE001"])
    assert len(notes) == 1 and "live masking exemption changed" in notes[0][1]
