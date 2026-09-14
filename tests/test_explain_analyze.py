"""EXPLAIN ANALYZE gating: config toggle + read-only + no write / no
dangerous-function escape. EXPLAIN ANALYZE executes the wrapped statement,
so the rules are load-bearing."""
from dba_slack_bot import config as cfg
from dba_slack_bot import query_safety as qs


def _toggle_on(monkeypatch):
    def fake(key, default=None):
        if key == "allow_explain_analyze":
            return "on"
        if key == "ast_safety_enabled":
            return "on"
        return default if default is not None else ""
    monkeypatch.setattr(cfg, "get_setting", fake)


# --- _explain_wraps_read (pure) ---------------------------------------------

def test_wraps_read_true_for_reads():
    for q in ("EXPLAIN ANALYZE SELECT 1",
              "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM t WHERE id=1",
              "EXPLAIN ANALYZE VERBOSE SELECT * FROM t",
              "EXPLAIN (ANALYZE) VALUES (1)",
              "EXPLAIN ANALYZE WITH x AS (SELECT 1) SELECT * FROM x"):
        assert qs._explain_wraps_read(q), q


def test_wraps_read_false_for_writes():
    for q in ("EXPLAIN ANALYZE UPDATE t SET a=1 WHERE id=2",
              "EXPLAIN ANALYZE INSERT INTO t VALUES (1)",
              "EXPLAIN ANALYZE DELETE FROM t WHERE id=1",
              "EXPLAIN (ANALYZE) WITH x AS (DELETE FROM t RETURNING id) SELECT * FROM x"):
        assert not qs._explain_wraps_read(q), q


# --- analyze() gating -------------------------------------------------------

def test_explain_analyze_blocked_by_default():
    # conftest returns config defaults → allow_explain_analyze is off.
    assert qs.analyze("EXPLAIN ANALYZE SELECT 1").blocked


def test_plain_explain_always_allowed():
    rep = qs.analyze("EXPLAIN SELECT 1")
    assert not rep.blocked
    assert rep.main_tier == "ro"


def test_read_explain_analyze_allowed_when_on(monkeypatch):
    _toggle_on(monkeypatch)
    rep = qs.analyze("EXPLAIN ANALYZE SELECT * FROM t WHERE id=1")
    assert not rep.blocked
    assert rep.main_tier == "ro"
    assert not qs.analyze("EXPLAIN (ANALYZE, BUFFERS) SELECT 1").blocked


def test_write_explain_analyze_blocked_even_when_on(monkeypatch):
    _toggle_on(monkeypatch)
    for w in ("EXPLAIN ANALYZE UPDATE t SET a=1 WHERE id=2",
              "EXPLAIN ANALYZE INSERT INTO t VALUES (1)",
              "EXPLAIN ANALYZE DELETE FROM t WHERE id=1"):
        assert qs.analyze(w).blocked, w


def test_explain_analyze_with_literals_and_casts_not_falsely_blocked(monkeypatch):
    # Regression: the inner statement must reach the parser with its string
    # literals INTACT. Feeding the literal-stripped body to sqlglot turned
    # `= 'x'::uuid AND m IN ('a','b')` into `= ::uuid AND m IN (,)`, which
    # failed to parse and falsely blocked a perfectly valid read.
    _toggle_on(monkeypatch)
    q = ("EXPLAIN ANALYZE SELECT o.order_id FROM orders o "
         "WHERE o.user_id = '019b89c0-ae44-756e-9f09-a5f553293337'::uuid "
         "AND o.market IN ('ton-try','ton-usdt') AND o.status = 'COMPLETED' "
         "ORDER BY o.accepted_at DESC LIMIT 21;")
    rep = qs.analyze(q)
    assert not rep.blocked, rep.blockers
    assert rep.main_tier == "ro"


def test_explain_analyze_literal_write_keyword_is_still_a_read(monkeypatch):
    # A string literal that merely contains a write keyword must not trip
    # the parser or flip the read/write classification.
    _toggle_on(monkeypatch)
    assert not qs.analyze(
        "EXPLAIN ANALYZE SELECT 'DELETE FROM x' AS note FROM orders").blocked


def test_dangerous_function_inside_explain_analyze_blocked_when_on(monkeypatch):
    # ANALYZE runs the inner query; sqlglot parses the whole EXPLAIN as an
    # opaque Command, so the inner statement is scanned separately.
    _toggle_on(monkeypatch)
    assert qs.analyze("EXPLAIN ANALYZE SELECT pg_read_file('/etc/passwd')").blocked
    assert qs.analyze(
        "EXPLAIN ANALYZE SELECT * FROM dblink('h','select 1') AS t(a int)").blocked
