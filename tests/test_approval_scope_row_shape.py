"""What a scope check needs in the row it is handed, and what happens without it.

The worst bug of the `access_model_v2` switch, reported the same day by two
people who could not approve things they plainly had the authority for.

`access.can_approve` read `request["required_tier"]` directly. A missing key
became `""`, whose rank came from `_TIER_RANK.get(tier, 99)` -- 99, ABOVE every
ceiling -- so the loop skipped every role row carrying a `max_tier` and returned
"no authority anywhere". Three callers passed a four-column row, so under the
new model nobody with a tier ceiling could approve anything from Slack: an
RO-ceiling admin was refused an RO request, and so was every pod lead. Only
unscoped super-admins, who have no ceiling to skip, still worked, which is why
it looked like "some people" rather than like a broken gate.

It was invisible before the switch because the legacy check derives the tier
when the column is absent (`admins.request_tier`) -- and `can_approve`'s own
docstring claimed it behaved "exactly as the old check does". Two
implementations of one security-relevant rule; the tests below pin the one.
"""
import inspect
import re

import pytest

from queryhub import access, admins

NEEDED = {"required_tier", "engine", "query", "target_server_id",
          "requester_slack_id"}


# --- the rule has one implementation ----------------------------------------


def test_the_new_model_derives_the_tier_instead_of_reading_the_column():
    """The fix that closes the class rather than the three instances: a caller
    that forgets the column gets the right answer instead of a silent no."""
    src = inspect.getsource(access.can_approve)
    assert "admins.request_tier(request)" in src
    assert 'request.get("required_tier")' not in src


def test_both_models_ask_the_same_function():
    """`admins.request_tier` resolves the engine before classifying, which is
    the SEC-ENG fix: a T-SQL statement read by the Postgres parser can look
    read-only and be admitted below its true tier. A second copy of that rule
    in the new model is a second place for it to rot."""
    assert admins.request_tier is admins._request_tier
    assert "engine" in inspect.getsource(admins.request_tier)


def test_the_persisted_tier_still_wins_over_the_derived_one():
    """It was classified with the target's engine at submit and a client
    cannot tamper with it. Deriving is the fallback, not the preference."""
    assert admins.request_tier({"required_tier": "ddl",
                                "query": "select 1"}) == "ddl"


def test_a_row_with_no_tier_column_is_still_answered():
    assert admins.request_tier({"query": "select 1", "engine": "postgres"}) == "ro"


# --- and every caller carries the columns anyway -----------------------------


@pytest.mark.parametrize("module,func", [
    ("queryhub.slack_app.handlers", "_guard_admin"),
    ("queryhub.slack_app.handlers", "_bundle_pending_items_in_scope"),
])
def test_the_slack_loaders_select_what_the_scope_check_reads(module, func):
    """Belt as well as braces. The derivation makes a short row work; loading
    the column makes it one read instead of a query per item, and makes the
    dependency visible to whoever edits the SELECT next."""
    import importlib
    src = inspect.getsource(getattr(importlib.import_module(module), func))
    for col in ("required_tier", "engine"):
        assert col in src, f"{func} does not load {col}"


def test_the_bundle_item_scope_row_carries_the_tier():
    """A bundle DM decides per item whether to show buttons. Without the tier
    every ceiling-bearing admin saw a view-only bundle."""
    from queryhub.slack_app import notifications
    src = inspect.getsource(notifications)
    i = src.index("item_for_scope = {")
    block = src[i:i + 500]
    assert '"required_tier"' in block and '"engine"' in block


def test_the_bundle_item_query_returns_the_tier():
    """`bundles.list_items` feeds that dict; the column has to exist to be
    copied."""
    from queryhub import bundles
    src = inspect.getsource(bundles.list_items)
    assert "required_tier" in src


def test_the_loader_docstring_no_longer_claims_more_than_it_does():
    """It said it loaded "the fields admins.can_approve() looks at" while
    omitting the one whose absence broke it. A comment that is wrong is worse
    than none, because it stops the next person looking."""
    from queryhub.slack_app import handlers
    doc = handlers._bundle_pending_items_in_scope.__doc__ or ""
    assert "required_tier" in doc


def test_the_needed_columns_are_exactly_what_the_checks_read():
    """A guard on the guard: if a scope check starts reading a new field, this
    fails and the loaders above have to be revisited."""
    read = set()
    for fn in (access.can_approve, admins._scope_admits, admins.request_tier):
        read |= set(re.findall(r'request\.get\("(\w+)"\)', inspect.getsource(fn)))
    assert read == NEEDED, f"scope checks now read {sorted(read)}"


# --- a ceiling nobody can read is not a wide ceiling -------------------------


def test_an_unreadable_ceiling_admits_nothing_in_either_model():
    """`_TIER_RANK.get(ceiling, 99)` made a typo the widest row in the table:
    `request_rank > 99` is false for every request, so an unparseable ceiling
    approved everything instead of nothing. Migration 116 stops the row
    existing; these keep one that already does from deciding."""
    for fn in (access.can_approve, admins._scope_admits):
        src = inspect.getsource(fn)
        assert "ceiling is None" in src, fn.__name__
        assert 'get(r["max_tier"], 99)' not in src
        assert 'get(scope["max_tier"], 99)' not in src


def test_the_new_model_constrains_the_tier_vocabulary_like_the_old_one():
    """`admins.max_tier` has always had CHECK (... IN ('ro','rw','ddl')). The
    table that replaced it was written with the wildcard and role checks and
    not that one."""
    import pathlib
    mig = (pathlib.Path(__file__).resolve().parents[1]
           / "migrations" / "116_role_tier_vocabulary.sql").read_text(encoding="utf-8")
    assert "role_assignment_max_tier_check" in mig
    assert "'ro', 'rw', 'ddl'" in mig
    assert "DROP CONSTRAINT IF EXISTS" in mig       # idempotent re-run


# --- being an admin is one way to have authority, not a prerequisite --------
#
# The second half of the same report. `_guard_admin` ran `is_admin` as a hard
# gate BEFORE the scope check, so the scope check was unreachable for the only
# people it was added for: a pod lead was DM'd about a request from their own
# pod, on their own pod's database, and told "you are not an authorized admin"
# when they pressed Approve. The notification became scope-aware on the day the
# roles went live and the button did not.


def test_a_request_in_hand_is_gated_on_can_approve_not_on_is_admin():
    """With a request, the question is authority over THAT request, and
    `can_approve` answers it whole: an admin satisfies it fleet-wide, a scoped
    approver inside their scope."""
    from queryhub.slack_app import handlers
    src = inspect.getsource(handlers._guard_admin)
    # the admin gate survives, but only on the branch with no request
    i = src.index("if req is None:")
    assert "admins.is_admin(user_id)" in src[i:]
    assert "admins.is_admin(user_id)" not in src[:i], \
        "is_admin is a precondition again"
    assert "elif not admins.can_approve(user_id, req):" in src


def test_an_administrative_action_still_needs_an_admin():
    """Approving an endpoint request or acting on a whole bundle carries no
    request to be scoped against, so `is_admin` remains the gate there. Six
    call sites pass no request_id and must not have been widened."""
    from queryhub.slack_app import handlers
    src = inspect.getsource(handlers._guard_admin)
    assert "if request_id is not None:" in src
    assert "if req is None:" in src


def test_a_request_that_has_gone_away_falls_back_to_the_admin_gate():
    """Its scope cannot be judged, so the fallback is the stricter question,
    not none. Previously a vanished request skipped the scope check entirely
    while the admin gate had already passed."""
    from queryhub.slack_app import handlers
    doc = handlers._guard_admin.__doc__ or ""
    assert "gone away" in doc


def test_the_refusal_tells_a_scoped_approver_which_refusal_it_is():
    """"You are not an authorized admin" reads as a bug to a pod lead who
    approves for their own team every day, and "outside your scope" reads as
    nonsense to somebody with no scope at all."""
    from queryhub.slack_app import handlers
    src = inspect.getsource(handlers._guard_admin)
    assert "admins.has_approval_authority(user_id)" in src
    assert "outside your approval scope" in src


def test_approval_authority_is_flag_aware():
    """Under the legacy model there is no approver who is not an admin, so
    there is no third state to describe and the helper must not invent one."""
    src = inspect.getsource(admins.has_approval_authority)
    assert "if not _v2():" in src
    assert "return False" in src
    assert '"admin", "approver"' in src
