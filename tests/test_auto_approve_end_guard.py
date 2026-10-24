"""An auto-approve row is written only by a function that checks its end and tier.

An auto-approve rule skips the review of a query. The application gives every
rule it creates an end date, at most `auto_approve_max_days` from now
(`auto_approve_limits.check_end`), and a tier of RO or RW, never DDL
(`check_tier`). The database enforces neither. The checks are calls in each
function that writes such a row, and nothing else makes a new writer call them.

This test parses every module under src/ and finds each string that is an
`INSERT INTO auto_approve_grants`, or an `INSERT INTO access_grant` whose column
list names `auto_approve`. It fails when the function that holds the string
does not call both checks, or when the string sits outside a function, where
this test cannot tell who calls them.

A statement that is built from variables is not found. The second test reads
the real tree, and it must find every writer that is known today, so that a
scan that reads nothing cannot pass.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

PACKAGE = "queryhub"
SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE_DIR = SRC / PACKAGE

_AUTO_TABLE = re.compile(r"insert\s+into\s+auto_approve_grants\b")
_GRANT_TABLE = re.compile(r"insert\s+into\s+access_grant\s*\(([^)]*)\)")

CHECK_MODULE = "auto_approve_limits"
# The calls that count as "this function checks the end":
#   - `check_end` or `copied_end` on the `auto_approve_limits` module
#   - `_checked_end`, the routes' wrapper of it (web/routes_admin.py)
#   - `check_waiver_end`, the grants module's wrapper of it
END_CHECKS = frozenset({"check_end", "copied_end", "_checked_end", "check_waiver_end"})
# ... and "checks the tier":
#   - `check_tier` on the module, or `_checked_tier`, the routes' wrapper
#   - `check_waiver_tier`, which refuses DDL through the module
#   - a copy that skips DDL by name is exempt from this half: see TIER_BY_COPY
TIER_CHECKS = frozenset({"check_tier", "_checked_tier", "check_waiver_tier"})
# Functions that copy a rule from a source that already holds it. They keep
# the source's tier and skip a DDL source by hand, which `check_tier` cannot
# say for a copy. Each entry names its reason.
TIER_BY_COPY = {
    "admin_copy_access": "skips a copied rule whose max_tier is ddl and counts it "
                         "(autoApproveSkippedDdl)",
}
# `decide_window` refuses tier ddl by its own code (`ddl`), and files no row for
# it. It checks the end by calling `check_end`.
TIER_BY_OWN_CODE = {
    "decide_window": "refuses a ddl window request (WindowDecisionRefused code ddl)",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    function: str


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else None
    return None


def _text(node: ast.AST) -> str:
    """The literal text of a string expression: a constant, the constant parts
    of an f-string, or a `+` of those. Anything else reads as empty."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text(node.left) + _text(node.right)
    return ""


def is_auto_approve_insert(sql: str) -> bool:
    flat = " ".join(sql.lower().split())
    if _AUTO_TABLE.search(flat):
        return True
    m = _GRANT_TABLE.search(flat)
    return bool(m) and "auto_approve" in [c.strip() for c in m.group(1).split(",")]


def _calls(fn: ast.AST, names: frozenset[str]) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        parts = (_dotted(node.func) or "").split(".")
        if parts[-1] in names and (len(parts) == 1 or parts[0] in (CHECK_MODULE, "grants")
                                   or parts[-1].startswith("_")):
            return True
    return False


def checks_the_end(fn: ast.AST) -> bool:
    return _calls(fn, END_CHECKS)


def checks_the_tier(fn: ast.AST) -> bool:
    return _calls(fn, TIER_CHECKS)


class _Scan(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.stack: list[ast.AST] = []
        # (where, end checked, tier checked)
        self.found: list[tuple[Finding, bool, bool]] = []

    def _function(self, node):
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def _record(self, node: ast.AST, expr: ast.AST) -> None:
        if not is_auto_approve_insert(_text(expr)):
            return
        if not self.stack:
            self.found.append((Finding(self.path, node.lineno, "<module>"), False, False))
            return
        # The outermost function decides: a nested helper may rely on a check
        # that its parent made.
        outer = self.stack[0]
        name = self.stack[-1].name
        tier_ok = checks_the_tier(outer) or name in TIER_BY_COPY or name in TIER_BY_OWN_CODE \
            or outer.name in TIER_BY_COPY or outer.name in TIER_BY_OWN_CODE
        self.found.append((Finding(self.path, node.lineno, name),
                           checks_the_end(outer), tier_ok))

    def visit_Call(self, node: ast.Call):
        for arg in node.args:
            self._record(node, arg)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        # A statement kept in a variable or a module constant, then passed on.
        self._record(node, node.value)
        self.generic_visit(node)


def scan(source: str, path: str = "<memory>") -> list[tuple[Finding, bool, bool]]:
    s = _Scan(path)
    s.visit(ast.parse(source))
    return s.found


def _scan_tree() -> list[tuple[Finding, bool, bool]]:
    out: list[tuple[Finding, bool, bool]] = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        out += scan(path.read_text(encoding="utf-8"), f"src/{rel}")
    return out


def test_every_function_that_writes_an_auto_approve_row_checks_its_end():
    bad = [f for f, end, _tier in _scan_tree() if not end]
    lines = "\n".join(f"  {f.path}:{f.line}  in {f.function}()" for f in bad)
    assert not bad, (
        "These functions write an auto-approve row and do not call the end "
        f"check:\n{lines}\n"
        "Call `auto_approve_limits.check_end` (or `_checked_end` in the web "
        "routes, or `grants.check_waiver_end`) before the INSERT. An auto-approve "
        "rule made in the app needs an end date. A rule made with SQL does not: "
        "see docs/OPERATIONS.md.")


def test_every_function_that_writes_an_auto_approve_row_checks_its_tier():
    bad = [f for f, _end, tier in _scan_tree() if not tier]
    lines = "\n".join(f"  {f.path}:{f.line}  in {f.function}()" for f in bad)
    assert not bad, (
        "These functions write an auto-approve row and do not call the tier "
        f"check:\n{lines}\n"
        "Call `auto_approve_limits.check_tier` (or `_checked_tier` in the web "
        "routes) before the INSERT. An auto-approve rule made in the app is RO "
        "or RW, never DDL.")


def test_the_scanner_reads_the_real_tree_and_finds_every_known_writer():
    """A scan that reads nothing passes everything. These functions write an
    auto-approve row today."""
    names = {f.function for f, _e, _t in _scan_tree()}
    known = {"_insert_user_waiver", "_insert_team_waiver", "_write_waivers",
             "admin_add_fleet_grant", "admin_copy_access", "decide_window"}
    assert known <= names, sorted(known - names)


def test_the_exemptions_name_functions_that_still_write_a_row():
    names = {f.function for f, _e, _t in _scan_tree()}
    assert set(TIER_BY_COPY) | set(TIER_BY_OWN_CODE) <= names


WRITE = "cur.execute('INSERT INTO auto_approve_grants (slack_user_id) VALUES (%s)', (u,))\n"


@pytest.mark.parametrize("source, expected", [
    # A writer with both checks.
    ("def f(cur):\n    auto_approve_limits.check_end(e)\n    auto_approve_limits.check_tier(t)\n"
     "    " + WRITE, [("f", True, True)]),
    # A writer with neither. This is the case that must fail the guard.
    ("def f(cur):\n    " + WRITE, [("f", False, False)]),
    # The end only, and the tier only.
    ("def f(cur):\n    _checked_end(e)\n    " + WRITE, [("f", True, False)]),
    ("def f(cur):\n    _checked_tier(t)\n    " + WRITE, [("f", False, True)]),
    # The wrappers of the routes and of the grants module.
    ("def f(cur):\n    grants.check_waiver_end(t, e)\n    grants.check_waiver_tier(m, t)\n"
     "    " + WRITE, [("f", True, True)]),
    # A copy: the end is capped by copied_end, the tier is exempt by name.
    ("def admin_copy_access(cur):\n    auto_approve_limits.copied_end(e)\n    " + WRITE,
     [("admin_copy_access", True, True)]),
    # A check on another module is not the check.
    ("def f(cur):\n    other.check_end(e)\n    other.check_tier(t)\n    " + WRITE,
     [("f", False, False)]),
    # Whitespace and case do not hide the statement.
    ("def f(cur):\n"
     "    cur.execute('INSERT   INTO\\n   AUTO_APPROVE_GRANTS (a) VALUES (1)')\n",
     [("f", False, False)]),
    # access_grant: a waiver row, in a plain string and in an f-string.
    ("def f(cur):\n"
     "    cur.execute('INSERT INTO access_grant (team_id, tier, auto_approve, valid_until) '\n"
     "                'VALUES (%s, %s, TRUE, %s)', p)\n", [("f", False, False)]),
    ("def f(cur, until):\n"
     "    cur.execute('INSERT INTO access_grant (team_id, auto_approve) '\n"
     "                f'VALUES (%s, TRUE, {until})', p)\n", [("f", False, False)]),
    # access_grant: an ordinary grant is not a waiver.
    ("def f(cur):\n"
     "    cur.execute('INSERT INTO access_grant (team_id, tier, valid_until) VALUES (%s, %s, %s)', p)\n",
     []),
    # A column that only contains the word is not the column.
    ("def f(cur):\n"
     "    cur.execute('INSERT INTO access_grant (team_id, auto_approve_note) VALUES (%s, %s)', p)\n",
     []),
    # The statement joined with + and a helper's text in the middle.
    ("def f(cur):\n"
     "    cur.execute('INSERT INTO access_grant (a, auto_approve) VALUES (%s, %s, ' + sub() + ')', p)\n",
     [("f", False, False)]),
    # A statement kept in a variable: it counts for the function that holds it.
    ("def f(cur):\n"
     "    sql = 'INSERT INTO auto_approve_grants (a) VALUES (1)'\n"
     "    cur.execute(sql)\n", [("f", False, False)]),
    # Outside a function the scanner cannot tell who calls the checks: a finding.
    ("SQL = 'INSERT INTO auto_approve_grants (a) VALUES (1)'\n", [("<module>", False, False)]),
    # The checks in a function that holds a nested one.
    ("def f(cur):\n    _checked_end(e)\n    _checked_tier(t)\n"
     "    def g():\n        cur.execute('INSERT INTO auto_approve_grants (a) VALUES (1)')\n    g()\n",
     [("g", True, True)]),
    # A read is not a write.
    ("def f(cur):\n    cur.execute('SELECT * FROM auto_approve_grants')\n", []),
    ("def f(cur):\n    cur.execute('UPDATE auto_approve_grants SET enabled = %s', (x,))\n", []),
])
def test_the_scanner_finds_each_writer_and_what_it_checks(source, expected):
    got = [(f.function, end, tier) for f, end, tier in scan(source)]
    assert got == expected
