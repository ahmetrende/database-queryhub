"""The reserved request id on the Slack side of the same feature.

The web half was easy: one tab, one reservation. The Slack modal is rebuilt from
scratch in FOUR places — target switch, template load, edit-and-resubmit, and the
batch/single toggle — and each rebuild used to write `private_metadata` as a
fresh `json.dumps({...})`. A key that writer did not know about was silently
dropped, which is harmless for `target_id` (every writer sets it) and wrong for a
reserved id set once at open: changing the target five times would have reserved
five ids and shown a different number each time.

Hence `modal.merge_pm`, and hence these tests: the property that matters is not
"the id appears" but "the id SURVIVES a rebuild that knows nothing about it".
"""
import json

import pytest

from queryhub.slack_app import handlers, modal


def _context_texts(view):
    return [e["text"] for b in view["blocks"] if b.get("type") == "context"
            for e in b.get("elements", []) if e.get("type") == "mrkdwn"]


def test_the_modal_shows_the_reserved_id():
    assert any("#2010" in t for t in _context_texts(modal.build_modal(req_id=2010)))


def test_without_an_id_there_is_no_placeholder():
    """A reservation can fail — /sql opening matters more than a number, so the
    modal must not advertise a blank one."""
    assert not any("Request" in t for t in _context_texts(modal.build_modal()))


@pytest.mark.parametrize("existing,updates,expect", [
    ('{"req_id": 7, "target_id": 1}', {"target_id": 52}, {"req_id": 7, "target_id": 52}),
    ('{"req_id": 7}', {"database": "appdb"}, {"req_id": 7, "database": "appdb"}),
    (None, {"req_id": 9}, {"req_id": 9}),
    ("", {"req_id": 9}, {"req_id": 9}),
    ("not json at all", {"req_id": 9}, {"req_id": 9}),
    ('["a list"]', {"req_id": 9}, {"req_id": 9}),
])
def test_merge_pm_keeps_what_it_does_not_know_about(existing, updates, expect):
    assert json.loads(modal.merge_pm(existing, **updates)) == expect


def test_a_target_switch_does_not_lose_the_id():
    """The exact regression: rebuild for a new target, id intact."""
    pm = modal.merge_pm(None, req_id=2010)
    after = modal.merge_pm(pm, target_id=52, database="appdb", query="select 1")
    assert json.loads(after)["req_id"] == 2010


def test_the_view_helper_stamps_the_id_without_clobbering_pm():
    view = {"private_metadata": json.dumps({"target_id": 3})}
    out = handlers._with_req_id(view, 2010)
    pm = json.loads(out["private_metadata"])
    assert pm == {"target_id": 3, "req_id": 2010}


def test_the_view_helper_is_a_no_op_without_an_id():
    view = {"private_metadata": '{"target_id": 3}'}
    assert handlers._with_req_id(view, None)["private_metadata"] == '{"target_id": 3}'


def test_the_submitted_modal_surfaces_the_id_for_claiming():
    """parse_submission is the seam between the modal and core_submit; if it
    drops req_id the reservation is silently wasted and the number changes."""
    view = {
        "private_metadata": json.dumps({"target_id": 52, "req_id": 2010}),
        "state": {"values": {
            modal.B_SERVER: {modal.A_SERVER: {
                "selected_option": {"value": "52"}}},
            modal.B_QUERY: {modal.A_QUERY: {"value": "select 1 from t"}},
            # parse_submission indexes this one directly, so a fixture without
            # it raises KeyError rather than testing anything.
            modal.B_JUSTIFICATION: {modal.A_JUSTIFICATION: {"value": "why"}},
        }},
    }
    parsed = modal.parse_submission(view)
    assert parsed["req_id"] == 2010
    assert parsed["target_server_id"] == 52


def test_a_modal_from_before_this_feature_parses_with_no_id():
    """Backward compatibility: an open modal at deploy time has no req_id, and
    submitting it must work rather than raise."""
    view = {
        "private_metadata": json.dumps({"target_id": 52}),
        "state": {"values": {
            modal.B_SERVER: {modal.A_SERVER: {
                "selected_option": {"value": "52"}}},
            modal.B_QUERY: {modal.A_QUERY: {"value": "select 1 from t"}},
            # parse_submission indexes this one directly, so a fixture without
            # it raises KeyError rather than testing anything.
            modal.B_JUSTIFICATION: {modal.A_JUSTIFICATION: {"value": "why"}},
        }},
    }
    assert modal.parse_submission(view)["req_id"] is None


def test_a_corrupt_private_metadata_does_not_break_submitting():
    view = {
        "private_metadata": "{{{",
        "state": {"values": {
            modal.B_SERVER: {modal.A_SERVER: {
                "selected_option": {"value": "52"}}},
            modal.B_QUERY: {modal.A_QUERY: {"value": "select 1 from t"}},
            # parse_submission indexes this one directly, so a fixture without
            # it raises KeyError rather than testing anything.
            modal.B_JUSTIFICATION: {modal.A_JUSTIFICATION: {"value": "why"}},
        }},
    }
    assert modal.parse_submission(view)["req_id"] is None


def test_the_submit_handler_claims_the_parsed_id():
    """Asserted on the source: reaching the real handler needs the whole Bolt
    request, and what matters here is that the id is passed at all."""
    import inspect
    src = inspect.getsource(handlers)
    assert "draft_id=parsed.get(\"req_id\")" in src


def test_every_pm_writer_merges_rather_than_replaces():
    """The guard for the whole class of bug: a future rebuild that goes back to
    json.dumps would drop the id again, silently."""
    import inspect
    import re
    src = inspect.getsource(handlers)
    # `private_metadata` assignments in the single-query modal flow
    bad = re.findall(r'\["private_metadata"\]\s*=\s*json\.dumps', src)
    assert not bad, ("a private_metadata writer replaces the whole dict; use "
                     "modal.merge_pm so keys it does not know about survive")
