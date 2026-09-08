"""Importing an org structure into the access model.

`scripts/import_teams.py` turns a CSV of "who is in which team" into `team` /
`team_member` rows. It is the one path by which an outside directory reaches
the authorization model, so what is pinned here is mostly what it REFUSES to
do — the reconcile logic is exercised against a fake cursor, and the three
prohibitions are read off the source, because each one is a sentence somebody
could delete without a test failing otherwise.

The company-specific half — whatever produces the CSV — deliberately lives
outside this repo. The CSV is the whole interface.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "scripts" / "import_teams.py").read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location(
        "import_teams", ROOT / "scripts" / "import_teams.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["import_teams"] = mod
    spec.loader.exec_module(mod)
    return mod


it = _load()


class FakeCur:
    """Just enough cursor for `plan`: canned answers, in call order."""

    def __init__(self, teams=(), members=None, roles=(), clashes=()):
        self._teams = [dict(r) for r in teams]
        self._members = members or {}
        self._roles = [dict(r) for r in roles]
        self._clashes = [dict(r) for r in clashes]
        self._out = []
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        if "FROM team " in sql and "source = %s" in sql:
            self._out = self._teams
        elif "FROM team_member" in sql and "principal_id, is_lead" in sql:
            self._out = self._members.get(params[0], [])
        elif "role_assignment" in sql:
            self._out = self._roles
        elif "source <> %s" in sql:
            self._out = self._clashes
        else:
            self._out = []

    def fetchall(self):
        return self._out

    def fetchone(self):
        return self._out[0] if self._out else None


def csv_at(tmp_path, text):
    p = tmp_path / "teams.csv"
    p.write_text(text, encoding="utf-8")
    return p


# --- reading the CSV ---------------------------------------------------------


def test_the_two_required_columns_are_named_when_they_are_missing(tmp_path):
    with pytest.raises(SystemExit) as e:
        it.read_csv(csv_at(tmp_path, "team,person\na,b\n"))
    assert "email" in str(e.value)


def test_display_name_and_is_lead_are_optional(tmp_path):
    names, members, bad = it.read_csv(csv_at(tmp_path, "team,email\nx,a@b.c\n"))
    assert names == {"x": "x"}           # falls back to the code
    assert members == {"x": [("a@b.c", False)]}
    assert bad == []


def test_a_row_missing_either_key_is_reported_rather_than_guessed(tmp_path):
    names, members, bad = it.read_csv(
        csv_at(tmp_path, "team,email\nx,\n,a@b.c\ny,c@d.e\n"))
    assert list(names) == ["y"]
    assert len(bad) == 2


def test_emails_are_lowercased_because_that_is_how_they_are_matched(tmp_path):
    _, members, _ = it.read_csv(csv_at(tmp_path, "team,email\nx,A@B.C\n"))
    assert members["x"] == [("a@b.c", False)]


@pytest.mark.parametrize("word,expected", [
    ("yes", True), ("Y", True), ("true", True), ("1", True), ("lead", True),
    ("no", False), ("", False), ("maybe", False)])
def test_the_lead_column_reads_the_obvious_spellings(tmp_path, word, expected):
    _, members, _ = it.read_csv(
        csv_at(tmp_path, f"team,email,is_lead\nx,a@b.c,{word}\n"))
    assert members["x"][0][1] is expected


# --- what it refuses to do ---------------------------------------------------


def test_it_never_creates_a_principal():
    """Being named in an org chart is not a request for access. A sync that
    can add people to the access model is a sync that can be used to."""
    assert "INSERT INTO principal" not in SRC
    assert "unresolved" in SRC


def test_it_never_writes_a_grant_or_a_role():
    """These teams say who works together. What they may reach is a separate
    decision, made by a person — otherwise the org chart becomes a permission
    surface."""
    for table in ("access_grant", "role_assignment", "principal_setting"):
        assert f"INSERT INTO {table}" not in SRC, table


def test_it_cannot_touch_the_hand_made_structure():
    """`manual` is what people create by hand AND what the migration-109
    mirror maintains from the legacy tables. An import reaching into it would
    fight the mirror and lose, repeatedly."""
    assert 'a.source == "manual"' in SRC
    assert "SELECT id, name, display_name FROM team " in SRC
    i = SRC.index("SELECT id, name, display_name FROM team ")
    assert "source = %s" in SRC[i:i + 200], "the listing is not scoped by source"


def test_membership_changes_do_not_notify_by_default():
    """A team from here carries no grants, so "you were added to team X" would
    announce a change to somebody's access that did not happen."""
    assert "app.auth_dm_suppress" in SRC
    assert '"--notify"' in SRC


# --- the reconcile -----------------------------------------------------------


def plan(**kw):
    cur = kw.pop("cur")
    return it.plan(cur, "src", kw.pop("names"), kw.pop("members"),
                   kw.pop("by_email"), kw.pop("keep_empty", False))


def test_a_team_nobody_here_belongs_to_is_not_created():
    """Every one is another row in the Teams list and another option in the
    picker that scopes an approver. The next run creates it the moment
    somebody in it has an account."""
    p = plan(cur=FakeCur(), names={"a": "A", "b": "B"},
             members={"a": [("x@y.z", False)], "b": [("nobody@y.z", False)]},
             by_email={"x@y.z": 1})
    assert p["add_teams"] == ["a"]
    assert p["unresolved"] == ["nobody@y.z"]


def test_keep_empty_creates_them_anyway():
    p = plan(cur=FakeCur(), names={"b": "B"},
             members={"b": [("nobody@y.z", False)]}, by_email={},
             keep_empty=True)
    assert p["add_teams"] == ["b"]


def test_an_existing_team_that_empties_out_is_not_dropped():
    """It is still a team. Deleting it would silently un-scope any approver
    pointed at it, and the CSV still lists it."""
    cur = FakeCur(teams=[{"id": 7, "name": "a", "display_name": "A"}],
                  members={7: [{"principal_id": 1, "is_lead": False}]})
    p = plan(cur=cur, names={"a": "A"}, members={"a": [("gone@y.z", False)]},
             by_email={})
    assert p["drop_teams"] == []
    assert p["del_members"] == [("a", 1)]


def test_a_team_gone_from_the_csv_is_dropped():
    cur = FakeCur(teams=[{"id": 7, "name": "old", "display_name": "Old"}])
    p = plan(cur=cur, names={}, members={}, by_email={})
    assert p["drop_teams"] == ["old"]


def test_a_team_a_live_role_is_scoped_to_is_kept_and_reported():
    """Somebody decided "approves for this team". Dropping it turns that row
    into a scope matching nobody, silently."""
    cur = FakeCur(teams=[{"id": 7, "name": "old", "display_name": "Old"}],
                  roles=[{"name": "old", "n": 2}])
    p = plan(cur=cur, names={}, members={}, by_email={})
    assert p["drop_teams"] == []
    assert p["pinned"] == [("old", 2)]


def test_a_code_another_source_owns_is_refused_by_name():
    """`team_name_uq` spans every source, so the alternative is a unique
    violation mid-transaction."""
    cur = FakeCur(clashes=[{"name": "a", "source": "manual"}])
    with pytest.raises(SystemExit) as e:
        plan(cur=cur, names={"a": "A"}, members={"a": [("x@y.z", False)]},
             by_email={"x@y.z": 1})
    assert "another source" in str(e.value) and "manual" in str(e.value)


def test_a_lead_flag_that_changed_is_an_update_not_a_re_add():
    cur = FakeCur(teams=[{"id": 7, "name": "a", "display_name": "A"}],
                  members={7: [{"principal_id": 1, "is_lead": False}]})
    p = plan(cur=cur, names={"a": "A"}, members={"a": [("x@y.z", True)]},
             by_email={"x@y.z": 1})
    assert p["add_members"] == [] and p["del_members"] == []
    assert p["lead_changes"] == [("a", 1, True)]


def test_a_renamed_display_name_is_an_update_not_a_new_team():
    cur = FakeCur(teams=[{"id": 7, "name": "a", "display_name": "Old"}])
    p = plan(cur=cur, names={"a": "New"}, members={"a": [("x@y.z", False)]},
             by_email={"x@y.z": 1})
    assert p["rename"] == ["a"] and p["add_teams"] == []


def test_a_second_run_over_unchanged_input_plans_nothing():
    """The whole point of a reconcile: it runs now, and again next month."""
    cur = FakeCur(teams=[{"id": 7, "name": "a", "display_name": "A"}],
                  members={7: [{"principal_id": 1, "is_lead": True}]})
    p = plan(cur=cur, names={"a": "A"}, members={"a": [("x@y.z", True)]},
             by_email={"x@y.z": 1})
    assert (p["add_teams"], p["rename"], p["drop_teams"]) == ([], [], [])
    assert (p["add_members"], p["del_members"], p["lead_changes"]) == ([], [], [])
