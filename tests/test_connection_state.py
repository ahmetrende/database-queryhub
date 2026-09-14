"""Why a saved query or a history row will not open.

A tab pointing at a target the caller cannot reach fails the same way for three
different reasons — the server was retired, the grant went away, or the row is
gone — and each one is a different screen. The payload could not tell them
apart: two produced a real-looking alias the sidebar simply did not contain,
and the third produced the numeric id as a string, which looks like an alias
too. The UI could only render a dash.
"""
from queryhub.web import mapping


def _hist(tid=3):
    return {"id": 1, "query": "SELECT 1", "target_server_id": tid,
            "database_name": "d", "status": "completed", "row_count": 1}


def _saved(tid=3):
    return {"id": 1, "query": "SELECT 1", "target_server_id": tid,
            "database_name": "d", "label": "mine"}


def test_history_reports_the_state_when_asked():
    e = mapping.history_entry(_hist(), lambda t: "prod-main", lambda t: "retired")
    assert e["connectionState"] == "retired"
    # The alias is still there — the UI needs the NAME to say what retired.
    assert e["connectionId"] == "prod-main"


def test_saved_reports_the_state_when_asked():
    e = mapping.saved_entry(_saved(), lambda t: "prod-main", lambda t: "no_access")
    assert e["connectionState"] == "no_access"


def test_a_deleted_target_still_says_gone_even_though_the_id_looks_like_a_name():
    """`history_entry` falls back to str(target_server_id) when the alias is
    unknown, which renders as a plausible connection name. The state is the
    only way to know it is not one."""
    e = mapping.history_entry(_hist(4242), lambda t: None, lambda t: "gone")
    assert e["connectionId"] == "4242"
    assert e["connectionState"] == "gone"


def test_a_saved_query_with_no_target_at_all():
    e = mapping.saved_entry(_saved(None), lambda t: None, lambda t: "none")
    assert e["connectionId"] is None
    assert e["connectionState"] == "none"


def test_omitting_the_resolver_keeps_the_old_shape():
    """Additive: a caller that does not pass one produces the same body it
    always did, with no `connectionState` key to confuse a stale client."""
    h = mapping.history_entry(_hist(), lambda t: "prod-main")
    s = mapping.saved_entry(_saved(), lambda t: "prod-main")
    assert "connectionState" not in h
    assert "connectionState" not in s
