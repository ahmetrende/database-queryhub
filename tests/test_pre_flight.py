"""pre_flight — plan analysis + human formatting (no live DB / EXPLAIN)."""
from dba_slack_bot import pre_flight as pf


def _plan(root):
    """Wrap a Plan node the way EXPLAIN (FORMAT JSON) does."""
    return [{"Plan": root}]


# --- _cost_band -------------------------------------------------------------

def test_cost_band_boundaries():
    assert pf._cost_band(0) == "XS"
    assert pf._cost_band(99) == "XS"
    assert pf._cost_band(100) == "S"
    assert pf._cost_band(999) == "S"
    assert pf._cost_band(1_000) == "M"
    assert pf._cost_band(49_999) == "M"
    assert pf._cost_band(50_000) == "L"
    assert pf._cost_band(999_999) == "L"
    assert pf._cost_band(1_000_000) == "XL"


# --- _fmt_bytes / _fmt_int --------------------------------------------------

def test_fmt_bytes():
    assert pf._fmt_bytes(0) == "0 B"
    assert pf._fmt_bytes(512) == "512 B"
    assert pf._fmt_bytes(2048) == "2.0 KB"
    assert pf._fmt_bytes(357_000_000) == "340 MB"


def test_fmt_int():
    assert pf._fmt_int(12) == "12"
    assert pf._fmt_int(1_200) == "1.2K"
    assert pf._fmt_int(5_200_000) == "5.2M"


# --- analyze_plan -----------------------------------------------------------

def test_analyze_plan_none():
    assert pf.analyze_plan(None) is None
    assert pf.analyze_plan([]) is None
    assert pf.analyze_plan(["not a dict"]) is None


def test_analyze_plan_clean_index_scan():
    a = pf.analyze_plan(_plan({
        "Node Type": "Index Scan",
        "Total Cost": 8.3,
        "Plan Rows": 12,
        "Plan Width": 100,
    }))
    assert a["est_rows"] == 12
    assert a["est_bytes"] == 1200
    assert a["cost_band"] == "XS"
    assert a["seq_scans"] == []
    assert a["flags"] == []
    assert a["has_risk"] is False


def test_analyze_plan_large_seq_scan_flagged():
    a = pf.analyze_plan(_plan({
        "Node Type": "Seq Scan",
        "Relation Name": "transactions",
        "Total Cost": 200.0,
        "Plan Rows": 5_000_000,
        "Plan Width": 68,
    }))
    assert ("transactions", 5_000_000) in a["seq_scans"]
    assert "seq_scan_large" in a["flags"]
    assert a["has_risk"] is True


def test_analyze_plan_high_cost_flagged():
    a = pf.analyze_plan(_plan({
        "Node Type": "Hash Join",
        "Total Cost": 60_000.0,
        "Plan Rows": 100,
        "Plan Width": 8,
    }))
    assert "high_cost" in a["flags"]
    assert a["has_risk"] is True


def test_analyze_plan_walks_children_for_seq_scan():
    a = pf.analyze_plan(_plan({
        "Node Type": "Aggregate",
        "Total Cost": 10.0,
        "Plan Rows": 1,
        "Plan Width": 8,
        "Plans": [{
            "Node Type": "Seq Scan",
            "Relation Name": "big",
            "Plan Rows": 200_000,
            "Plan Width": 4,
        }],
    }))
    assert ("big", 200_000) in a["seq_scans"]
    assert "seq_scan_large" in a["flags"]


# --- risk_summary_text ------------------------------------------------------

def test_risk_summary_none_for_no_plan():
    assert pf.risk_summary_text(None) is None


def test_risk_summary_clean_plan_is_barchart():
    txt = pf.risk_summary_text(_plan({
        "Node Type": "Index Scan",
        "Total Cost": 8.3,
        "Plan Rows": 12,
        "Plan Width": 100,
    }))
    assert txt.startswith(":bar_chart:")
    assert "Index Scan" in txt
    assert "`XS`" in txt


def test_risk_summary_seq_scan_is_warning():
    txt = pf.risk_summary_text(_plan({
        "Node Type": "Seq Scan",
        "Relation Name": "transactions",
        "Total Cost": 200.0,
        "Plan Rows": 5_000_000,
        "Plan Width": 68,
    }))
    assert txt.startswith(":warning:")
    assert "transactions" in txt


# --- is_explainable ---------------------------------------------------------

def test_is_explainable_dml_yes_ddl_no():
    assert pf.is_explainable("INSERT INTO t VALUES (1)")
    assert pf.is_explainable("DELETE FROM t WHERE id=1")
    assert not pf.is_explainable("CREATE TABLE t (id int)")
    assert not pf.is_explainable("GRANT SELECT ON t TO r")
    assert not pf.is_explainable("")


def test_is_explainable_skips_explain_queries():
    # An EXPLAIN can't be pre-EXPLAIN'd (nested EXPLAIN is a syntax error),
    # so pre-flight must skip it — otherwise a valid EXPLAIN ANALYZE SELECT
    # submission is falsely rejected.
    assert not pf.is_explainable("EXPLAIN ANALYZE SELECT count(*) FROM t")
    assert not pf.is_explainable("EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM t")
    assert not pf.is_explainable("EXPLAIN SELECT 1")


# --- explain_write_estimate guards (return before any DB connection) --------

def test_write_estimate_only_for_rw():
    # ro / ddl never get a write estimate.
    assert pf.explain_write_estimate(1, "db", "ro", "UPDATE t SET a=1") is None
    assert pf.explain_write_estimate(1, "db", "ddl", "DROP TABLE t") is None


def test_write_estimate_skips_multi_statement():
    # The tail of a multi-statement payload must never reach EXPLAIN.
    assert pf.explain_write_estimate(
        1, "db", "rw", "UPDATE a SET x=1; DELETE FROM b") is None


def test_write_estimate_skips_non_write_and_empty():
    assert pf.explain_write_estimate(1, "db", "rw", "SELECT 1") is None
    assert pf.explain_write_estimate(1, "db", "rw", "") is None


# --- _affected_rows: ModifyTable reports 0 at the root without RETURNING -----

def test_affected_rows_reads_child_of_modify_table():
    # An UPDATE/DELETE without RETURNING: root ModifyTable Plan Rows is 0,
    # the real estimate is the child scan that feeds it.
    plan = [{"Plan": {
        "Node Type": "ModifyTable", "Operation": "Update", "Plan Rows": 0,
        "Plans": [{"Node Type": "Seq Scan", "Relation Name": "t", "Plan Rows": 74}],
    }}]
    assert pf._affected_rows(plan) == 74


def test_affected_rows_falls_back_to_root_with_returning():
    # With RETURNING the root carries a non-zero count; keep it if there is
    # no child estimate to prefer.
    plan = [{"Plan": {"Node Type": "ModifyTable", "Plan Rows": 12, "Plans": []}}]
    assert pf._affected_rows(plan) == 12


def test_affected_rows_none_for_garbage():
    assert pf._affected_rows(None) is None
    assert pf._affected_rows([]) is None
