"""The request row IS the executor's input.

`core_submit.REQUEST_RETURNING` names the columns that every submit and
decision path hands to `executor.submit()`. A column the executor reads but
that clause omits does not raise — `dict.get` returns None and the executor
makes a quietly wrong decision with it.

That is not hypothetical. `requests.unmasked` arrived in migration 093 and was
never added to the list, so `request.get("unmasked")` was None on every path:
a super-admin who asked for real values got a masked result, while the audit
row recorded the intent faithfully. Nothing failed, no test covered it, and the
only visible symptom was a redacted column in a result grid.

So this pins the contract itself rather than any one column.
"""
import ast
import re
from pathlib import Path

from queryhub import core_submit

SRC = Path(__file__).resolve().parents[1] / "src" / "queryhub"


def _returned_columns() -> set[str]:
    return {c.strip() for c in core_submit.REQUEST_RETURNING.split(",")}


def _keys_read_from(path: Path, var: str) -> set[str]:
    """Every literal key read off `var` — both `var["k"]` and `var.get("k")`."""
    tree = ast.parse(path.read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        # var["k"]
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name) and node.value.id == var
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            keys.add(node.slice.value)
        # var.get("k")
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == var
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
    return keys


def test_executor_reads_only_columns_the_submit_path_returns():
    read = _keys_read_from(SRC / "executor.py", "request")
    missing = sorted(read - _returned_columns())
    assert not missing, (
        "executor.py reads these keys off the request row, but "
        "core_submit.REQUEST_RETURNING does not return them, so they arrive as "
        f"None: {missing}. Add them to the constant."
    )


def test_unmasked_is_returned():
    """The specific regression, named so a failure says what broke."""
    assert "unmasked" in _returned_columns(), (
        "requests.unmasked must be returned to the executor — without it a "
        "super-admin's unmasked result is silently masked."
    )


def test_no_submit_path_hand_rolls_its_own_returning_list():
    """Two call sites used to spell the list out and drifted from the shared
    constant. Any RETURNING over `requests` must interpolate it instead."""
    text = (SRC / "core_submit.py").read_text()
    hand_rolled = [
        m.group(0)[:60]
        for m in re.finditer(r'RETURNING\s+(?!\{REQUEST_RETURNING\})'
                             r'id,\s*requester_slack_id', text)
    ]
    assert not hand_rolled, (
        f"hand-rolled RETURNING list(s) found: {hand_rolled}. "
        "Use f\"RETURNING {REQUEST_RETURNING}\" so the list cannot drift."
    )
