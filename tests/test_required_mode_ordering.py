"""`required_mode()` reports "ro" for a BLOCKED query, so callers must check.

The wrapper is documented as returning `main_tier` "if not rep.blocked else 'ro'"
(query_safety.required_mode). That flattening is a safe default for a display
label and a trap for an authorization decision: a mixed-tier submission, an
`UPDATE` with no `WHERE`, or an unparseable fragment all come back as read-only
when the wrapper is read on its own.

Every authorization path gets this right today — each one calls `analyze()` and
returns on `.blocked` before it looks at the tier. Nothing enforced that, so the
next caller added to one of these functions would not find out. These tests read
the real source of the real functions, so they fail when the order changes or a
blocked check is dropped.

The remaining callers are display-only and deliberately not listed here:
web/mapping.py (tier labels on API rows), slack_app/modal.py (filtering history
to RO for re-run), slack_app/notifications.py (a tier label in a Slack message).
A blocked query is never stored, so they cannot receive one.
"""
import ast
import importlib
import inspect
import textwrap

import pytest

# (module, attribute) for every function that turns a tier into an authorization
# or execution decision.
AUTHZ_CALLERS = [
    ("queryhub.core_submit", "validate_submission"),
    ("queryhub.slack_app.handlers", "_validate_batch_item"),
    ("queryhub.web.routes_queries", "explain_query"),
]
# `classify_query` is NOT here on purpose — it reads `analyze().main_tier`
# directly and so never meets the flattening. Its own test is below; a skip in
# this list would have read like a hole rather than a design choice.


def _source(module: str, attr: str) -> str:
    return inspect.getsource(getattr(importlib.import_module(module), attr))


def _code_only(module: str, attr: str) -> str:
    """The function body with its docstring removed.

    These functions explain themselves at length, and `admins._request_tier`'s
    docstring names `required_mode()` several lines above the code that prefers
    the persisted tier. Searching the raw source put the prose first and failed a
    correct implementation — measure the code, not the commentary."""
    src = textwrap.dedent(_source(module, attr))
    fn = ast.parse(src).body[0]
    doc = ast.get_docstring(fn, clean=False)
    return src.replace(doc, "", 1) if doc else src


@pytest.mark.parametrize("module,attr", AUTHZ_CALLERS)
def test_blocked_is_checked_before_the_tier_is_used(module, attr):
    src = _code_only(module, attr)
    if "required_mode(" not in src:
        pytest.skip(f"{attr} no longer reads required_mode")
    blocked_at = min(
        (i for i, line in enumerate(src.splitlines()) if ".blocked" in line),
        default=None)
    tier_at = min(
        (i for i, line in enumerate(src.splitlines())
         if "required_mode(" in line), default=None)
    assert blocked_at is not None, (
        f"{module}.{attr} reads required_mode() without ever checking "
        "analyze().blocked — a blocked query reports as read-only")
    assert blocked_at < tier_at, (
        f"{module}.{attr} looks at the tier (line {tier_at}) before it checks "
        f"blocked (line {blocked_at}); the flattening applies in between")


def test_the_approval_scope_prefers_the_persisted_tier():
    """`admins._request_tier` is the odd one out: it does not call analyze(), it
    reads the tier PERSISTED at submit time (SEC-ENG) and only derives as a
    fallback for rows written before that column existed. Those rows already
    passed the submit-time blocked check, which is why deriving is safe here —
    but only while the persisted value is preferred."""
    src = _code_only("queryhub.admins", "_request_tier")
    persisted_at = src.index("required_tier")
    derived_at = src.index("required_mode(")
    assert persisted_at < derived_at, (
        "the approval scope check derives the tier before consulting the "
        "persisted one, so a blocked query could be admitted at 'ro'")


def test_classify_reads_the_report_and_reports_blocked_separately():
    """The endpoint behind the editor's tier chip and `tierExceedsGrant`.

    It takes the tier from `analyze().main_tier` rather than the wrapper, so the
    blocked-to-ro flattening cannot reach it, and it returns `blocked` alongside
    `tierExceedsGrant` so the client is told both facts instead of inferring one
    from the other. Both halves matter: a client that saw only the tier would
    show a green RO chip for a query the submit path will refuse."""
    src = _code_only("queryhub.web.routes_queries", "classify_query")
    assert "required_mode(" not in src, (
        "classify_query started using the wrapper — add it to AUTHZ_CALLERS "
        "above so the ordering is checked")
    assert "safety.main_tier" in src
    assert '"blocked"' in src and '"tierExceedsGrant"' in src, (
        "the classify response must carry both, or the UI cannot tell a blocked "
        "query apart from an allowed one")


def test_the_wrapper_still_flattens_blocked_to_ro():
    """The behaviour the tests above exist for. If this ever stops being true,
    they are protecting nothing and should be revisited rather than deleted."""
    from queryhub import query_safety

    blocked = "SELECT 1;\nUPDATE t SET x = 1 WHERE id = 1;"   # mixed-tier
    report = query_safety.analyze(blocked, engine="postgres")
    assert report.blocked is True, "expected a mixed-tier submission to be blocked"
    assert query_safety.required_mode(blocked, engine="postgres") == "ro", (
        "required_mode no longer flattens blocked to 'ro' — good, but the "
        "ordering tests above were written for that behaviour")


def test_the_docstring_warns_about_authorization():
    """A future caller skims the docstring, not this file."""
    from queryhub import query_safety

    doc = (query_safety.required_mode.__doc__ or "").lower()
    assert "authoriz" in doc, (
        "required_mode's docstring must say its answer is not an authorization "
        "input on its own")
