"""When a justification is required, and who gets to decide that.

The field has two readers, and that is the whole argument. The first is the
approver, deciding; an auto-approved request has no approver, so demanding prose
there asks for something nobody reads at decision time. The second is whoever
opens the audit log later asking why a write ran — and that reader is still
served, because an auto-approved submission records `grant_id` and the grant
carries its own `reason`, written by the admin who issued it.

Two failure modes are pinned here.

The first is drift. `POST /classify` publishes this answer so the editor can show
the field only when it is needed, and the editor is not allowed its own copy of
the rule — it had one, and it was wrong in two directions at once: DDL-only when
RW needs one too, and blind to auto-approval entirely. A field built against that
value would appear for the wrong statements and demand prose nobody reads. So
there is one function, and the endpoint calls it.

The second is the scheduled exemption. A scheduled request must NEVER be exempt:
its grant may lapse before the run time, in which case create_request falls back
to normal approval, a human is in the loop after all, and the reason is needed.
"""
import pytest

from queryhub import core_submit as cs


# ---------------------------------------------------------------------------
# the rule itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["rw", "ddl"])
def test_writes_need_a_reason_when_a_human_will_read_it(monkeypatch, tier):
    monkeypatch.setattr(cs.cfg, "get_bool", lambda k, d=False: d)
    assert cs.needs_justification(tier, will_auto_approve=False) is True


@pytest.mark.parametrize("tier", ["ro", "rw", "ddl"])
def test_auto_approved_requests_need_none(monkeypatch, tier):
    """Including DDL. If a grant covers it, the grant's own reason is the record."""
    monkeypatch.setattr(cs.cfg, "get_bool", lambda k, d=False: d)
    assert cs.needs_justification(tier, will_auto_approve=True) is False


def test_reads_follow_the_config(monkeypatch):
    monkeypatch.setattr(cs.cfg, "get_bool", lambda k, d=False: False)
    assert cs.needs_justification("ro", will_auto_approve=False) is False
    monkeypatch.setattr(cs.cfg, "get_bool", lambda k, d=False: True)
    assert cs.needs_justification("ro", will_auto_approve=False) is True


def test_the_config_cannot_re_impose_it_on_an_auto_approved_read(monkeypatch):
    """`require_justification` is about giving an approver context. There is no
    approver here, so the toggle has nothing to act on."""
    monkeypatch.setattr(cs.cfg, "get_bool", lambda k, d=False: True)
    assert cs.needs_justification("ro", will_auto_approve=True) is False


# ---------------------------------------------------------------------------
# the endpoint must not carry a second copy
# ---------------------------------------------------------------------------

def test_classify_publishes_the_rule_rather_than_reimplementing_it():
    """The regression this prevents is a literal one: the endpoint used to
    return `required == "ddl"`. Anything that computes the answer inline is
    free to be wrong in a way nothing above would catch."""
    import inspect

    from queryhub.web import routes_queries

    src = inspect.getsource(routes_queries.classify_query)
    assert "core_submit.needs_justification" in src, (
        "POST /classify must delegate to core_submit.needs_justification")
    assert 'required == "ddl"' not in src, (
        "the old inline rule is back — it is wrong for RW and ignores "
        "auto-approval")


def test_classify_reports_the_scheduled_case_separately():
    """The client knows whether a schedule was picked and the endpoint does not,
    so it publishes both answers and the UI chooses. Dropping the second one
    would make a scheduled RW look exempt to a UI that trusted the first."""
    import inspect

    from queryhub.web import routes_queries

    src = inspect.getsource(routes_queries.classify_query)
    assert "requiresJustificationWhenScheduled" in src
    # ...and it must be the non-exempt answer: `will_auto` must not be passed
    # through for the scheduled variant.
    assert "needs_justification(required, False)" in src


# ---------------------------------------------------------------------------
# the submit path, at the point of decision
# ---------------------------------------------------------------------------

def test_submit_never_exempts_a_scheduled_request():
    """Read at the source, because the exemption is computed inline in
    validate_submission and reaching it needs a target, a grant and a live
    database. The guard is a single condition and this asserts it exists: a
    schedule present means the auto-approve lookup is not even attempted."""
    import inspect

    src = inspect.getsource(cs.validate_submission)
    assert "not (schedule_date or schedule_time)" in src, (
        "the scheduled guard is gone — a scheduled write whose grant lapses "
        "before the run time would reach an approver with no reason attached")


def test_submit_fails_closed_when_the_grant_lookup_raises():
    """An auto-approve lookup that errors must leave the requirement standing,
    not drop it. Same fail-closed direction as every other gate here."""
    import inspect

    src = inspect.getsource(cs.validate_submission)
    body = src[src.index("auto_approve_exempt = False"):]
    assert "except Exception" in body
    assert "auto_approve_exempt = False" in body[body.index("except Exception"):]
