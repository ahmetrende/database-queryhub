"""What the person-first access screen asks the API for.

Four asks came back from the design round, and each one names a defect in the
surface rather than a missing feature:

  * granting the same access to five people was five calls, and five calls can
    leave three written — a half-applied authorization that the grants table
    cannot describe, because it records what exists and never what was meant;
  * `copy-access` only ever ADDED, so "make this person match that one" was a
    sentence the API could not say, and auto-approve — the grant that skips
    human review — was silently never copied either way;
  * the people list was `requesters WHERE enabled`, so the picker could not
    offer the disabled person whose access an admin came to clean up;
  * `scopeTargets` reported NULL for "every target" and `[]` for "none", two
    opposite meanings told apart by the shape of a value.
"""
import pytest

from queryhub.web import routes_admin as ra


# --- several people, one call, all or nothing --------------------------------

def test_a_bad_id_in_the_list_writes_nothing(monkeypatch):
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, s: "UADMIN")
    monkeypatch.setattr(ra, "_target_id_of", lambda alias: 7)
    monkeypatch.setattr(ra.grants, "control_plane_target_ids", lambda: set())

    def boom(**kw):
        raise AssertionError("nothing may be written when an id is refused")
    monkeypatch.setattr(ra.grants, "grant_many", boom)

    body = ra.GrantIn(subjectType="user", subjects=["U0AAAAAAAAA", "not-an-id"],
                      connectionId="svc-prod", tier="ro")
    with pytest.raises(Exception) as e:
        ra.admin_create_grant(body, claims={"sub": "UADMIN"})
    assert "not-an-id" in str(e.value.detail if hasattr(e.value, "detail") else e.value)


def test_every_named_person_is_granted_once(monkeypatch):
    seen = {}
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, s: "UADMIN")
    monkeypatch.setattr(ra, "_target_id_of", lambda alias: 7)
    monkeypatch.setattr(ra.grants, "control_plane_target_ids", lambda: set())
    monkeypatch.setattr(ra, "_slack_profile", lambda pid: {"name": pid})
    monkeypatch.setattr(ra.db, "fetch_one", lambda *a, **k: {"name": "Someone"})

    def fake_many(**kw):
        seen.update(kw)
        return [{"grantee_id": g, "mode": "ro", "databases": None,
                 "whitelisted_now": False} for g, _ in kw["grantees"]]
    monkeypatch.setattr(ra.grants, "grant_many", fake_many)

    # The duplicate is the point: a picker can hand the same person twice.
    out = ra.admin_create_grant(
        ra.GrantIn(subjectType="user", connectionId="svc-prod", tier="ro",
                   subjects=["U0AAAAAAAAA", "U0BBBBBBBBB", "U0AAAAAAAAA"]),
        claims={"sub": "UADMIN"})
    assert [g for g, _ in seen["grantees"]] == ["U0AAAAAAAAA", "U0BBBBBBBBB"]
    assert out["subjects"] == ["U0AAAAAAAAA", "U0BBBBBBBBB"]
    # The single-subject fields stay, so an existing client keeps working.
    assert out["subject"] == "U0AAAAAAAAA"


def test_one_subject_still_works_the_old_way(monkeypatch):
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, s: "UADMIN")
    monkeypatch.setattr(ra, "_target_id_of", lambda alias: 7)
    monkeypatch.setattr(ra.grants, "control_plane_target_ids", lambda: set())
    monkeypatch.setattr(ra, "_slack_profile", lambda pid: {})
    monkeypatch.setattr(ra.db, "fetch_one", lambda *a, **k: {"name": None})
    monkeypatch.setattr(ra.grants, "grant_many",
                        lambda **kw: [{"grantee_id": "U0AAAAAAAAA", "mode": "ro",
                                       "databases": None, "whitelisted_now": True}])
    out = ra.admin_create_grant(
        ra.GrantIn(subject="U0AAAAAAAAA", connectionId="svc-prod"),
        claims={"sub": "UADMIN"})
    assert out["subject"] == "U0AAAAAAAAA" and out["subjects"] == ["U0AAAAAAAAA"]


def test_a_team_grant_refuses_a_list(monkeypatch):
    # A team is already a set of people; a list would grant to the first only.
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, s: "UADMIN")
    monkeypatch.setattr(ra, "_target_id_of", lambda alias: 7)
    monkeypatch.setattr(ra.grants, "control_plane_target_ids", lambda: set())
    with pytest.raises(Exception) as e:
        ra.admin_create_grant(
            ra.GrantIn(subjectType="team", subjects=["data-eng", "platform"],
                       connectionId="svc-prod"),
            claims={"sub": "UADMIN"})
    assert "exactly one team" in str(getattr(e.value, "detail", e.value))


# --- the scope flags ---------------------------------------------------------

def test_all_and_none_are_separate_fields():
    """NULL is the wildcard and [] is 'none' — opposite meanings that used to
    reach the client as the shape of one value. A UI that reads meaning from an
    absence is wrong the first day that absence means something else."""
    import inspect
    src = inspect.getsource(ra.admin_effective_access)
    assert '"scopeTargetsAll": adm["scope_target_ids"] is None' in src
    assert '"scopeTeamsAll": adm["scope_team_ids"] is None' in src
    # and the raw arrays stay, so nothing that reads them today breaks
    assert '"scopeTargets": adm["scope_target_ids"]' in src


# --- copy-access modes -------------------------------------------------------

def test_the_mode_is_validated(monkeypatch):
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, s: "UADMIN")
    with pytest.raises(Exception) as e:
        ra.admin_copy_access("U0AAAAAAAAA",
                             ra.CopyAccessIn(source="U0BBBBBBBBB", mode="wipe"),
                             claims={"sub": "UADMIN"})
    assert "merge" in str(getattr(e.value, "detail", e.value))


def test_merge_is_the_default():
    # The mode that cannot take anything away is the one you get by not asking.
    assert ra.CopyAccessIn(source="U0BBBBBBBBB").mode == "merge"
    assert ra.CopyAccessIn(source="U0BBBBBBBBB").includeAutoApprove is False
    assert ra.CopyAccessIn(source="U0BBBBBBBBB").dryRun is False


# --- copy-access: replace, auto-approve, dry run -----------------------------

class _Cur:
    """Enough cursor to watch what a copy writes, revokes and previews."""
    def __init__(self, src_user=(), src_team=(), dest_existing=(), src_auto=()):
        self.src_user, self.src_team = list(src_user), list(src_team)
        self.dest_existing, self.src_auto = list(dest_existing), list(src_auto)
        self.rowcount = 1
        self.inserted, self.revoked, self.auto_inserted = [], [], []
        self.audit = {}
        self._rows = []
        self._seen_dest_read = False

    def execute(self, sql, params=()):
        if "SET LOCAL" in sql:
            self._rows = []
        elif "INSERT INTO requesters" in sql:
            self._rows = []
        elif sql.startswith("SELECT target_server_id, mode FROM user_target_grants"):
            self._rows = self.dest_existing          # what the destination has
        elif "FROM user_target_grants" in sql and sql.lstrip().startswith("SELECT"):
            self._rows = self.src_user
        elif "team_target_grants" in sql and sql.lstrip().startswith("SELECT"):
            self._rows = self.src_team
        elif "INSERT INTO team_members" in sql:
            self._rows = []
        elif "FROM teams WHERE id" in sql:
            self._rows = []
        elif "FROM auto_approve_grants" in sql:
            self._rows = self.src_auto
        elif "INSERT INTO auto_approve_grants" in sql:
            self.auto_inserted.append(params)
            self._rows = []
        elif "INSERT INTO user_target_grants" in sql:
            self.inserted.append(params)
            self._rows = []
        elif sql.startswith("UPDATE user_target_grants SET revoked_at"):
            self.revoked.append(params[1])
            self._rows = []
        elif "SELECT id, alias FROM target_servers" in sql:
            self._rows = [{"id": i, "alias": f"svc-{i}"} for i in (params[0] or [])]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _wire(monkeypatch, cur):
    import contextlib
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, s: "UADMIN")
    monkeypatch.setattr(ra.grants, "control_plane_target_ids", lambda: {99})
    monkeypatch.setattr(ra, "_slack_profile", lambda pid: {})
    monkeypatch.setattr(ra.audit, "log_in",
                        lambda c, r, u, n, a, d: cur.audit.update(d))

    @contextlib.contextmanager
    def txn():
        yield cur
    monkeypatch.setattr(ra.db, "transaction", txn)


def test_replace_revokes_what_the_source_does_not_have(monkeypatch):
    cur = _Cur(src_user=[{"target_server_id": 1, "mode": "ro",
                          "allowed_databases": None}],
               dest_existing=[{"target_server_id": 1, "mode": "ro"},
                              {"target_server_id": 2, "mode": "rw"}])
    _wire(monkeypatch, cur)
    out = ra.admin_copy_access("U0AAAAAAAAA",
                               ra.CopyAccessIn(source="U0BBBBBBBBB", mode="replace",
                                               includeTeams=True),
                               claims={"sub": "UADMIN"})
    assert cur.revoked == [2]
    assert out["revoked"] == [{"targetId": 2, "tier": "RW", "connectionId": "svc-2"}]
    assert out["mode"] == "replace"


def test_merge_never_revokes(monkeypatch):
    cur = _Cur(src_user=[{"target_server_id": 1, "mode": "ro",
                          "allowed_databases": None}],
               dest_existing=[{"target_server_id": 2, "mode": "rw"}])
    _wire(monkeypatch, cur)
    out = ra.admin_copy_access("U0AAAAAAAAA",
                               ra.CopyAccessIn(source="U0BBBBBBBBB"),
                               claims={"sub": "UADMIN"})
    assert cur.revoked == [] and out["revoked"] == []


def test_a_dry_run_writes_nothing_and_names_both_sides(monkeypatch):
    cur = _Cur(src_user=[{"target_server_id": 1, "mode": "ro",
                          "allowed_databases": None}],
               dest_existing=[{"target_server_id": 2, "mode": "rw"}])
    _wire(monkeypatch, cur)
    out = ra.admin_copy_access("U0AAAAAAAAA",
                               ra.CopyAccessIn(source="U0BBBBBBBBB", mode="replace",
                                               dryRun=True),
                               claims={"sub": "UADMIN"})
    assert out["dryRun"] is True
    assert out["wouldWrite"] == ["svc-1"]
    assert [r["connectionId"] for r in out["wouldRevoke"]] == ["svc-2"]
    assert cur.inserted == [] and cur.revoked == []


def test_auto_approve_is_copied_only_when_asked(monkeypatch):
    auto = [{"target_server_id": 5, "database_name": "nova",
             "max_tier": "ro", "expires_at": None}]
    cur = _Cur(src_auto=auto)
    _wire(monkeypatch, cur)
    out = ra.admin_copy_access("U0AAAAAAAAA",
                               ra.CopyAccessIn(source="U0BBBBBBBBB"),
                               claims={"sub": "UADMIN"})
    assert cur.auto_inserted == [] and out["autoApproveCopied"] == 0

    cur2 = _Cur(src_auto=auto)
    _wire(monkeypatch, cur2)
    out2 = ra.admin_copy_access("U0AAAAAAAAA",
                                ra.CopyAccessIn(source="U0BBBBBBBBB",
                                                includeAutoApprove=True),
                                claims={"sub": "UADMIN"})
    assert len(cur2.auto_inserted) == 1
    # A count for the toast, the names beside it — the same pair as
    # written / writtenTargets, because the panel reads one of each.
    assert out2["autoApproveCopied"] == 1
    assert out2["autoApproveCopiedTargets"] == ["svc-5/nova"]


def test_the_control_plane_is_never_copied_or_revoked(monkeypatch):
    cur = _Cur(src_user=[{"target_server_id": 99, "mode": "ddl",
                          "allowed_databases": None}],
               dest_existing=[{"target_server_id": 99, "mode": "ddl"}])
    _wire(monkeypatch, cur)
    out = ra.admin_copy_access("U0AAAAAAAAA",
                               ra.CopyAccessIn(source="U0BBBBBBBBB", mode="replace"),
                               claims={"sub": "UADMIN"})
    assert cur.inserted == [] and cur.revoked == [] and out["revoked"] == []


# --- the people list ---------------------------------------------------------

def test_the_directory_lists_everyone_an_admin_can_name(monkeypatch):
    """A name missing from the picker is not a name an admin gives up on: they
    type the id by hand, and a typo writes a grant against a principal that
    cannot sign in. So a disabled requester, an admin with no requesters row and
    a bare grant subject are all in the list, marked."""
    rows = [
        {"slack_user_id": "U_LIVE", "name": "Live One",
         "enabled": True, "kind": "requester"},
        {"slack_user_id": "U_DBA", "name": "Approver",
         "enabled": True, "kind": "admin"},
        {"slack_user_id": "U_GONE", "name": "Left Us",
         "enabled": False, "kind": "requester"},
        {"slack_user_id": "U_GRANTONLY", "name": None,
         "enabled": False, "kind": "grant_only"},
    ]
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, s: "UADMIN")
    monkeypatch.setattr(ra.db, "fetch_all", lambda *a, **k: rows)
    people = ra.admin_people(claims={"sub": "UADMIN"})["people"]

    assert [p["id"] for p in people] == ["U_LIVE", "U_DBA", "U_GONE", "U_GRANTONLY"]
    assert [p["enabled"] for p in people] == [True, True, False, False]
    assert [p["kind"] for p in people] == ["requester", "admin", "requester", "grant_only"]
    # A nameless principal still shows something you can pick.
    assert people[-1]["name"] == "U_GRANTONLY"


def test_the_query_reaches_all_four_sources(monkeypatch):
    seen = {}
    monkeypatch.setattr(ra.admin, "require_admin", lambda c, s: "UADMIN")
    monkeypatch.setattr(ra.db, "fetch_all",
                        lambda sql, *a, **k: seen.setdefault("sql", sql) and [] or [])
    ra.admin_people(claims={"sub": "UADMIN"})
    sql = seen["sql"]
    assert "FROM requesters" in sql
    assert "FROM admins a" in sql
    assert "user_target_grants" in sql and "auto_approve_grants" in sql
    # Disabled rows must NOT be filtered out any more.
    assert "WHERE enabled = TRUE" not in sql
    # Ordered so the live people come first and the rest are visibly last.
    assert "ORDER BY p.enabled DESC" in sql


def test_the_preview_counts_the_auto_approve_windows(monkeypatch):
    """The panel draws a line from `wouldCopyAutoApprove`. An absent field draws
    silence, which reads as "none" for the one option that skips human review."""
    cur = _Cur(src_auto=[{"target_server_id": 5, "database_name": None,
                          "max_tier": "ro", "expires_at": None}])
    # the count comes from its own query, so answer that one
    real_execute = cur.execute

    def execute(sql, params=()):
        if "count(*)" in sql and "auto_approve_grants" in sql:
            cur._rows = [{"n": 2}]
            return
        real_execute(sql, params)
    monkeypatch.setattr(cur, "execute", execute)
    _wire(monkeypatch, cur)

    out = ra.admin_copy_access("U0AAAAAAAAA",
                               ra.CopyAccessIn(source="U0BBBBBBBBB", dryRun=True,
                                               includeAutoApprove=True),
                               claims={"sub": "UADMIN"})
    assert out["wouldCopyAutoApprove"] == 2

    cur2 = _Cur()
    _wire(monkeypatch, cur2)
    out2 = ra.admin_copy_access("U0AAAAAAAAA",
                                ra.CopyAccessIn(source="U0BBBBBBBBB", dryRun=True),
                                claims={"sub": "UADMIN"})
    # Not asked for, so not counted — and still present rather than missing.
    assert out2["wouldCopyAutoApprove"] == 0
