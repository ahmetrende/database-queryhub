"""Audit insight: one rule produced most of the auto-approved queries."""
import re
from pathlib import Path

from queryhub import audit_insight as ai
from queryhub.web import routes_admin

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "src" / "queryhub" / "web" / "routes_admin.py").read_text(encoding="utf-8")


def _rows(tier, **counts):
    return [{"tier": tier, "grant_id": k, "n": v} for k, v in counts.items()]


def test_below_min_total_gives_nothing():
    assert ai.pick_insights(_rows("RO", a=ai.MIN_TOTAL - 1)) == []


def test_below_min_percent_gives_nothing():
    # 49 of 100 is 49 percent.
    assert ai.pick_insights(_rows("RO", a=49, b=26, c=25)) == []


def test_exactly_the_thresholds_give_an_entry():
    out = ai.pick_insights(_rows("RO", a=10, b=10))
    assert out == [{"tier": "RO", "total": 20, "ruleId": "a",
                    "ruleLabel": "rule a", "count": 10, "percent": 50}]


def test_percent_rounds_down():
    out = ai.pick_insights(_rows("RO", a=67, b=33, c=0))
    assert out[0]["percent"] == 67
    out = ai.pick_insights(_rows("RO", a=20, b=10))
    assert out[0]["percent"] == 66


def test_ro_and_rw_are_separate():
    rows = _rows("RO", a=30) + _rows("RW", b=5, c=5)
    out = ai.pick_insights(rows)
    assert [e["tier"] for e in out] == ["RO"]
    rows = _rows("RO", a=15) + _rows("RW", b=15)
    assert ai.pick_insights(rows) == []
    rows = _rows("RO", a=25) + _rows("RW", **{"ag:7": 40})
    out = ai.pick_insights(rows)
    assert [(e["tier"], e["ruleId"]) for e in out] == [("RO", "a"), ("RW", "ag:7")]


def test_tie_goes_to_the_smaller_id_string():
    out = ai.pick_insights(_rows("RO", **{"9": 15, "10": 15}))
    assert out[0]["ruleId"] == "10"
    assert out[0]["percent"] == 50


def test_row_without_rule_id_is_skipped():
    rows = [{"tier": "RO", "grant_id": None, "n": 100}] + _rows("RO", a=5)
    assert ai.pick_insights(rows) == []


def test_label_function_is_used():
    out = ai.pick_insights(_rows("RO", a=30),
                           label_of=lambda tier, rid: f"{rid}/{tier}")
    assert out[0]["ruleLabel"] == "a/RO"


# --- the SQL and the route --------------------------------------------------


def test_sql_counts_only_auto_approved_and_groups_by_tier_and_rule():
    sql = routes_admin._AUDIT_INSIGHT_SQL
    assert "action = 'auto_approved'" in sql
    assert "submitted" not in sql
    assert "JOIN requests q ON q.id = a.request_id" in sql
    assert "required_tier" in sql
    assert "details->>'grant_id' IS NOT NULL" in sql
    assert "INTERVAL '30 days'" in sql
    assert "GROUP BY 1, 2" in sql
    assert "basis" not in sql


def test_route_uses_the_audit_gate():
    m = re.search(r'@router\.get\("/audit/insight"\).*?(?=\n@router\.)', SRC, re.S)
    assert m
    assert 'require_admin(claims, "review")' in m.group(0)


def test_route_returns_the_shape(monkeypatch):
    gates = []
    monkeypatch.setattr(routes_admin.admin, "require_admin",
                        lambda claims, scope: gates.append(scope))

    class FakeDb:
        def fetch_all(self, sql, params=None):
            if "FROM audit_log" in sql:
                return [{"tier": "RO", "grant_id": "41", "n": 30},
                        {"tier": "RW", "grant_id": "ag:2", "n": 3}]
            return []  # the rule no longer exists

    monkeypatch.setattr(routes_admin, "db", FakeDb())
    out = routes_admin.admin_audit_insight(claims={"sub": "U0EXAMPLE001"})
    assert gates == ["review"]
    assert out == {"windowDays": 30, "insights": [
        {"tier": "RO", "total": 30, "ruleId": "41", "ruleLabel": "rule 41",
         "count": 30, "percent": 100}]}


def test_label_names_person_and_connection(monkeypatch):
    class FakeDb:
        def fetch_all(self, sql, params=None):
            return [{"user_name": "Jordan Ray", "slack_user_id": "U0EXAMPLE001",
                     "target_server_id": 5}]

    monkeypatch.setattr(routes_admin, "db", FakeDb())
    monkeypatch.setattr(routes_admin, "_alias_of", lambda t: "prod-main")
    assert routes_admin._auto_rule_label("12", "RO") == "Jordan Ray on prod-main (RO)"


def test_deleted_rule_and_bad_id_give_rule_label(monkeypatch):
    class FakeDb:
        def fetch_all(self, sql, params=None):
            return []

    monkeypatch.setattr(routes_admin, "db", FakeDb())
    assert routes_admin._auto_rule_label("12", "RO") == "rule 12"
    assert routes_admin._auto_rule_label("ag:3", "RW") == "rule ag:3"
    assert routes_admin._auto_rule_label("ag:x", "RW") == "rule ag:x"
