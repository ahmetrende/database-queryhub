"""A new approve path cannot skip the kill switch.

While the kill switch is on, every action that approves and runs a request is
refused (operator decision, 2026-10-24). `core_decide.require_kill_switch_off`
is the one check. This test parses every module under src/ and fails when:

  1. `core_decide.decide` or `core_decide.decide_bundle` does not call the
     check. Both make it themselves for an approve, so no caller can skip it.
  2. A function outside core_decide calls `core_decide.decide` or
     `decide_bundle` with an approve (or with a decision it cannot read) and
     does not handle `KillSwitchOn`. An unhandled one would be a 500 on the web
     and a silent failure in Slack. A literal "reject" or "changes" needs no
     handling: a deny is never refused.
  3. A function runs a request or an import (`executor.submit`,
     `executor.submit_import`) or approves a CSV import, and neither calls a
     kill switch check nor sits on the allow-list below with its reason.

The allow-list is for code that runs a request that was approved at submit.
The submit path has refused it already while the switch was on.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "queryhub"

CHECK_NAMES = {"require_kill_switch_off", "kill_switch_on", "_kill_switch_on"}

# (module path under src/queryhub, function name) -> why it is safe.
ALLOWED = {
    ("core_decide.py", "apply_effects"):
        "Runs after decide() or decide_bundle(). Both refuse an approve first.",
    ("core_submit.py", "dispatch_and_notify"):
        "Runs a request that was auto-approved at submit. "
        "validate_submission() refused every submit while the switch was on.",
    ("executor.py", "_import_run"):
        "The worker that claims an approved import. handle_import_approve() "
        "refuses the approval while the switch is on, and the submit is refused too.",
    ("web/routes_queries.py", "submit_batch"):
        "Runs batch items that were auto-approved at submit. "
        "validate_submission() refused them while the switch was on.",
}


def _functions():
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield rel, node


def _names(fn) -> set[str]:
    """Every name and attribute name used in the function."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _calls_on(fn, owner: str, methods: set[str]):
    """Calls shaped `owner.method(...)` for a method in `methods`."""
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == owner
                and n.func.attr in methods):
            yield n


def _decision_arg(call: ast.Call):
    """The decision argument as a string, or None when it is not a literal."""
    arg = None
    if len(call.args) >= 2:
        arg = call.args[1]
    for kw in call.keywords:
        if kw.arg == "decision":
            arg = kw.value
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _approves_csv_import(fn) -> bool:
    for n in ast.walk(fn):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "UPDATE csv_imports" in n.value and "'approved'" in n.value):
            return True
    return False


def _runs_work(fn) -> bool:
    return (any(_calls_on(fn, "executor", {"submit", "submit_import"}))
            or _approves_csv_import(fn))


def test_decide_and_decide_bundle_make_the_check_themselves():
    found = {}
    for rel, fn in _functions():
        if rel == "core_decide.py" and fn.name in ("decide", "decide_bundle"):
            found[fn.name] = "require_kill_switch_off" in _names(fn)
    assert found == {"decide": True, "decide_bundle": True}, found


def test_a_caller_of_decide_handles_the_refusal():
    bad = []
    for rel, fn in _functions():
        if rel == "core_decide.py":
            continue
        for call in _calls_on(fn, "core_decide", {"decide", "decide_bundle"}):
            if _decision_arg(call) in ("reject", "changes"):
                continue
            if "KillSwitchOn" not in _names(fn):
                bad.append(f"{rel}:{call.lineno} {fn.name}")
    assert not bad, (
        "These functions approve through core_decide and do not handle "
        f"core_decide.KillSwitchOn: {bad}")


def test_every_function_that_runs_work_checks_the_switch_or_says_why_not():
    bad = []
    for rel, fn in _functions():
        if not _runs_work(fn):
            continue
        if (rel, fn.name) in ALLOWED:
            continue
        if not (_names(fn) & CHECK_NAMES):
            bad.append(f"{rel} {fn.name}")
    assert not bad, (
        "These functions run a request or an import and never look at the kill "
        f"switch. Call core_decide.require_kill_switch_off() first: {bad}")


def test_the_allow_list_has_no_stale_entry():
    live = {(rel, fn.name) for rel, fn in _functions() if _runs_work(fn)}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, f"These allow-list entries match no function that runs work: {stale}"
