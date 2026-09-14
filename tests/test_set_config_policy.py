"""`set_config()` is SET by another name and obeys the same policy.

analyze()'s SET branch only fires when the LEADING word is SET, so the
function form used to bypass the entire allow-list and value policy:
`SELECT set_config('statement_timeout','0',false)` re-enabled an unlimited
timeout, `set_config('search_path',...)` undid the CVE-2018-1058 pin, and
`set_config('role', <other team's role>)` stepped out of the per-team
`SET LOCAL ROLE` fence — all as a plain RO submission eligible for
auto-approval.
"""
import pytest

from dba_slack_bot import query_safety as qs

BLOCKED = [
    # Value policy: 0 disables the safety timeout (same message as SET LOCAL).
    "SELECT set_config('statement_timeout','0',false)",
    # Name policy: role / search_path are not on the allow-list.
    "SELECT set_config('role','postgres',true)",
    "SELECT set_config('search_path','evil,pg_catalog',true)",
    # The qualified name resolves to the same function.
    "SELECT pg_catalog.set_config('role','team_b_ro',true)",
    "SELECT set_config('default_transaction_read_only','off',true)",
    # The real cross-team read: switch role, then read another team's tables.
    "SELECT set_config('role','team_b_ro',true); SELECT * FROM finance.salaries",
    # Computed arguments can't be policy-checked -> refused, not ignored.
    "SELECT set_config(pname, pval, true)",
]

ALLOWED = [
    # Exactly what `SET LOCAL work_mem = '64MB'` would be allowed to do.
    "SELECT set_config('work_mem','64MB',true)",
    "SELECT set_config('statement_timeout','30s',true)",
    # Reading a GUC is not a policy change.
    "SELECT current_setting('search_path')",
]


@pytest.mark.parametrize("sql", BLOCKED)
def test_set_config_bypasses_are_blocked(sql):
    report = qs.analyze(sql, engine="postgres")
    assert report.blocked, f"set_config bypass passed: {sql}"
    assert any("set_config" in b for b in report.blockers), report.blockers


@pytest.mark.parametrize("sql", ALLOWED)
def test_policy_compliant_set_config_is_allowed(sql):
    report = qs.analyze(sql, engine="postgres")
    assert not report.blocked, f"false positive on {sql}: {report.blockers}"


def test_statement_and_function_forms_agree(monkeypatch):
    # The two spellings of the same change must get the same verdict.
    stmt_form = qs.analyze("SET LOCAL statement_timeout = 0", engine="postgres")
    func_form = qs.analyze("SELECT set_config('statement_timeout','0',true)",
                           engine="postgres")
    assert stmt_form.blocked and func_form.blocked


def test_gate_is_independent_of_ast_safety_toggle(monkeypatch):
    from dba_slack_bot import ast_safety
    monkeypatch.setattr(ast_safety, "check", lambda sql, engine="postgres": [])
    report = qs.analyze("SELECT set_config('role','postgres',true)",
                        engine="postgres")
    assert report.blocked
