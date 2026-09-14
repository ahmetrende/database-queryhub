"""query_safety.analyze / required_mode — the static SQL safety gate."""
from queryhub import query_safety as qs


def analyze(sql):
    return qs.analyze(sql)


# --- tier classification ---------------------------------------------------

def test_select_is_ro():
    r = analyze("SELECT 1")
    assert not r.blocked
    assert r.main_tier == "ro"


def test_with_cte_select_is_ro():
    r = analyze("WITH c AS (SELECT 1) SELECT * FROM c")
    assert not r.blocked
    assert r.main_tier == "ro"


def test_insert_is_rw():
    r = analyze("INSERT INTO t (a) VALUES (1)")
    assert not r.blocked
    assert r.main_tier == "rw"


def test_update_with_where_is_rw():
    r = analyze("UPDATE t SET a = 1 WHERE id = 2")
    assert not r.blocked
    assert r.main_tier == "rw"


def test_create_table_is_ddl():
    r = analyze("CREATE TABLE t (id int)")
    assert not r.blocked
    assert r.main_tier == "ddl"


def test_required_mode_matches_tier():
    assert qs.required_mode("SELECT 1") == "ro"
    assert qs.required_mode("UPDATE t SET a=1 WHERE id=1") == "rw"
    assert qs.required_mode("DROP TABLE t") == "ddl"


# --- WHERE-required ---------------------------------------------------------

def test_update_without_where_blocked():
    r = analyze("UPDATE t SET a = 1")
    assert r.blocked


def test_delete_without_where_blocked():
    r = analyze("DELETE FROM t")
    assert r.blocked


def test_delete_with_where_ok():
    assert not analyze("DELETE FROM t WHERE id = 5").blocked


# --- tautology / always-true WHERE -----------------------------------------

def test_update_tautology_1eq1_blocked():
    assert analyze("UPDATE t SET a = 1 WHERE 1=1").blocked


def test_update_tautology_true_blocked():
    assert analyze("UPDATE t SET a = 1 WHERE true").blocked


def test_delete_or_1eq1_bypass_blocked():
    assert analyze("DELETE FROM t WHERE id = 5 OR 1=1").blocked


def test_padding_where_is_allowed():
    # WHERE 1=1 AND <real predicate> is a common, legitimate pattern.
    assert not analyze("UPDATE t SET a = 1 WHERE 1=1 AND id = 5").blocked


# --- leading-keyword allow-list / bans -------------------------------------

def test_copy_blocked():
    assert analyze("COPY t FROM '/etc/passwd'").blocked


def test_do_block_blocked():
    assert analyze("DO $$ BEGIN END $$").blocked


def test_begin_blocked():
    assert analyze("BEGIN").blocked


def test_unknown_leading_word_blocked():
    assert analyze("FROBNICATE everything").blocked


# --- multi-statement --------------------------------------------------------

def test_multi_statement_blocked():
    # Two real statements (not a SET prelude) must be rejected.
    assert analyze("SELECT 1; DROP TABLE t").blocked


# --- CTE-embedded DML -------------------------------------------------------

def test_cte_with_dml_blocked():
    # A top-level WITH whose body writes — classic tier-bypass; reject.
    sql = "WITH x AS (INSERT INTO t (a) VALUES (1) RETURNING a) SELECT * FROM x"
    assert analyze(sql).blocked


# --- EXPLAIN ANALYZE --------------------------------------------------------

def test_plain_explain_ok():
    assert not analyze("EXPLAIN SELECT 1").blocked


def test_explain_analyze_blocked():
    assert analyze("EXPLAIN ANALYZE SELECT 1").blocked


# --- destructive flagging ---------------------------------------------------

def test_destructive_flag_on_delete():
    r = analyze("DELETE FROM t WHERE id = 1")
    assert r.is_destructive
    assert "DELETE" in r.keywords_found


def test_select_not_destructive():
    assert not analyze("SELECT 1").is_destructive


# --- empty / whitespace -----------------------------------------------------

def test_empty_blocked():
    assert analyze("").blocked
    assert analyze("   ").blocked
