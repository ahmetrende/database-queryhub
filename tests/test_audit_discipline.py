"""A standalone audit row is for an event that changes no state.

`audit.log()` commits its row in a transaction of its own. When a state change
and its audit row are two commits, a failure between them leaves one without
the other: a changed password with no audit row, or half an IdP sync. So a path
that changes state writes `audit.log_in(cur, ...)` inside the transaction of
the change.

This test parses every module under src/ and finds each standalone call, under
any name the module has for `audit`. `routes_queries` imports it as
`audit_mod`, and a grep for `audit.log(` does not find those calls. The test
fails on a call whose action is not on the allow-list below. It also fails on a
call or a reference whose action it cannot read.

A second rule covers the same hole under another spelling: a literal
`INSERT INTO audit_log` that goes through `db.execute`, `db.fetch_one` or
`db.fetch_all`. Those run in a transaction of their own, just like
`audit.log()`. The same statement on a cursor (`cur.execute`) is fine, because
the cursor belongs to the transaction of the caller.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

PACKAGE = "queryhub"
SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE_DIR = SRC / PACKAGE

# The actions that may use the standalone form. Each one records an event and
# changes no state, so no commit can leave a change without its record. Every
# entry gives its reason.
INFORMATIONAL = frozenset({
    # A sign-in. The row is the access record of the sign-in. It changes no
    # permission and no setting.
    "web_login",
    # A sign-out. The row comes after the revocation, and its failure is only
    # logged. A failed audit insert must never keep a session alive.
    "web_signout",
    # The /sql modal opened in Slack. An access record. Nothing changes.
    "slack_sql_opened",
    # A stored result went to its owner as a file. An access record. Nothing
    # changes.
    "web_result_downloaded",
    # What an Athena query scanned, written after the run. It is best-effort,
    # so a failed write cannot lose the result that the user waits for.
    "athena_query_cost",
    # A super-admin query with masking off. The executor writes the row on
    # purpose BEFORE the query runs, so it exists even when the run fails.
    "result_unmasked",
})

UNREADABLE = "<not a string literal>"
REFERENCE = "<a reference, not a call>"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    action: str


def _dotted(node: ast.AST) -> str | None:
    """`a.b.c` for a chain of names, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else None
    return None


def _source_of(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    """The absolute module name that an import statement reads from."""
    if not node.level:
        return node.module or ""
    parts = module.split(".")
    base = parts if is_package else parts[:-1]
    base = base[:len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _aliases(tree: ast.Module, module: str,
             is_package: bool) -> tuple[set[str], set[str]]:
    """The names that this module has for the audit module, and the names
    that it has for `audit.log` itself."""
    modules = {f"{PACKAGE}.audit"}
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name == f"{PACKAGE}.audit" and name.asname:
                    modules.add(name.asname)
        elif isinstance(node, ast.ImportFrom):
            source = _source_of(node, module, is_package)
            for name in node.names:
                if source == PACKAGE and name.name == "audit":
                    modules.add(name.asname or "audit")
                elif source == f"{PACKAGE}.audit" and name.name == "log":
                    functions.add(name.asname or "log")
    return modules, functions


def _action(call: ast.Call) -> str:
    """The action argument as written, or UNREADABLE."""
    node = next((kw.value for kw in call.keywords if kw.arg == "action"), None)
    if node is None:
        first_four = call.args[:4]
        if len(first_four) < 4 or any(isinstance(a, ast.Starred) for a in first_four):
            return UNREADABLE
        node = first_four[3]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return UNREADABLE


def scan(source: str, module: str, path: str = "<memory>",
         is_package: bool = False) -> list[Finding]:
    """Every standalone `audit.log` call or reference in one module."""
    tree = ast.parse(source)
    modules, functions = _aliases(tree, module, is_package)

    def is_audit_log(node: ast.AST) -> bool:
        if isinstance(node, ast.Attribute) and node.attr == "log":
            return _dotted(node.value) in modules
        return isinstance(node, ast.Name) and node.id in functions

    found: list[Finding] = []
    called: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and is_audit_log(node.func):
            called.add(id(node.func))
            found.append(Finding(path, node.lineno, _action(node)))
    for node in ast.walk(tree):
        if is_audit_log(node) and id(node) not in called \
                and not isinstance(getattr(node, "ctx", None), ast.Store):
            found.append(Finding(path, node.lineno, REFERENCE))
    return sorted(found, key=lambda f: f.line)


def _standalone_calls() -> list[Finding]:
    found: list[Finding] = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        rel = path.relative_to(SRC)
        is_package = path.name == "__init__.py"
        parts = rel.with_suffix("").parts
        module = ".".join(parts[:-1] if is_package else parts)
        found += scan(path.read_text(encoding="utf-8"), module,
                      f"src/{rel.as_posix()}", is_package)
    return found


def test_every_standalone_audit_row_is_informational():
    bad = [f for f in _standalone_calls() if f.action not in INFORMATIONAL]
    lines = "\n".join(f"  {f.path}:{f.line}  {f.action!r}" for f in bad)
    assert not bad, (
        "These standalone audit.log() calls write an action that is not on "
        "the allow-list, or an action that this test cannot read:\n"
        f"{lines}\n"
        "State-changing actions use `audit.log_in` inside the transaction of "
        "the change. If an action changes no state, add it to INFORMATIONAL "
        "in tests/test_audit_discipline.py, with the reason.")


def test_every_allowed_action_is_still_written_standalone():
    """An entry with no call left behind it is a permission that nobody uses.
    Remove it, so that the list stays the true list."""
    stale = INFORMATIONAL - {f.action for f in _standalone_calls()}
    assert not stale, (
        f"No standalone audit.log() call writes {sorted(stale)}. Remove these "
        "actions from INFORMATIONAL in tests/test_audit_discipline.py.")


@pytest.mark.parametrize("source, module, expected", [
    ("from . import audit\naudit.log(None, 'U1', None, 'grant_changed')\n",
     "queryhub.grants", ["grant_changed"]),
    ("from .. import audit as audit_mod\n"
     "audit_mod.log(None, 'U1', None, 'cancelled', {})\n",
     "queryhub.web.routes_queries", ["cancelled"]),
    ("from .. import (db,\n    audit,\n)\naudit.log(1, 'U1', None, action='kw')\n",
     "queryhub.web.routes_admin", ["kw"]),
    ("from queryhub.audit import log as write\nwrite(1, 'U1', None, 'bare')\n",
     "queryhub.x", ["bare"]),
    ("from .audit import log\nlog(1, 'U1', None, 'relative')\n",
     "queryhub.x", ["relative"]),
    ("import queryhub.audit\nqueryhub.audit.log(1, 'U1', None, 'dotted')\n",
     "queryhub.x", ["dotted"]),
    ("from . import audit\naudit.log(None, 'U1', None, f'idp_{state}')\n",
     "queryhub.x", [UNREADABLE]),
    ("from . import audit\naudit.log(*row)\n", "queryhub.x", [UNREADABLE]),
    ("from . import audit\nwriter = audit.log\n", "queryhub.x", [REFERENCE]),
    ("from . import audit\naudit.log_in(cur, None, 'U1', None, 'atomic')\n",
     "queryhub.x", []),
    ("import logging\nlog = logging.getLogger(__name__)\nlog.info('a %s', 1)\n",
     "queryhub.x", []),
    ("from . import db\naudit = Recorder()\naudit.log(1, 'U1', None, 'other')\n",
     "queryhub.x", []),
])
def test_the_scanner_finds_every_way_to_reach_audit_log(source, module, expected):
    """The guard is only as good as its search. These are the spellings it
    must find, and the ones it must leave alone."""
    assert [f.action for f in scan(source, module)] == expected


def test_the_scanner_reads_the_real_tree():
    """A scan that reads nothing passes everything. The tree holds calls in
    more than one module, under more than one alias."""
    found = _standalone_calls()
    assert len({f.path for f in found}) >= 3, found
    assert any(f.path.endswith("web/routes_queries.py") for f in found), found


# --- a literal INSERT INTO audit_log outside a transaction -------------------

_AUTOCOMMIT_CALLS = frozenset({"db.execute", "db.fetch_one", "db.fetch_all"})
_RAW_INSERT = "insert into audit_log"


def scan_raw_inserts(source: str, path: str = "<memory>") -> list[Finding]:
    """Every call that runs a literal `INSERT INTO audit_log` in a transaction
    of its own."""
    found: list[Finding] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and _dotted(node.func) in _AUTOCOMMIT_CALLS
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            continue
        if _RAW_INSERT in " ".join(node.args[0].value.lower().split()):
            found.append(Finding(path, node.lineno, "INSERT INTO audit_log"))
    return found


def test_no_audit_row_is_written_by_a_raw_insert_outside_a_transaction():
    bad: list[Finding] = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        bad += scan_raw_inserts(path.read_text(encoding="utf-8"), f"src/{rel}")
    lines = "\n".join(f"  {f.path}:{f.line}" for f in bad)
    assert not bad, (
        "These calls insert an audit row with db.execute, db.fetch_one or "
        f"db.fetch_all, which commit on their own:\n{lines}\n"
        "Write the row with `audit.log_in(cur, ...)` or `cur.execute(...)` inside "
        "the transaction of the change.")


@pytest.mark.parametrize("source, expected", [
    ("from . import db\ndb.execute(\"INSERT INTO audit_log (action) VALUES (%s)\", (a,))\n", 1),
    ("from . import db\ndb.fetch_one(\n  \"insert  into\\n audit_log (action) \"\n"
     "  \"VALUES (%s) RETURNING id\", (a,))\n", 1),
    ("cur.execute(\"INSERT INTO audit_log (action) VALUES (%s)\", (a,))\n", 0),
    ("from . import db\ndb.execute(\"UPDATE requests SET status = 'x'\")\n", 0),
    ("from . import db\ndb.execute(_INSERT_SQL, params)\n", 0),
    ("from . import db\ndb.execute(\"INSERT INTO audit_log_archive (a) VALUES (1)\")\n", 1),
])
def test_the_raw_insert_scanner_finds_the_spellings_that_matter(source, expected):
    assert len(scan_raw_inserts(source)) == expected

