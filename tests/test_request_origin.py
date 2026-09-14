"""Which door a request came through has to survive a third door.

`requests.origin` is plain text with a `'slack'` default and no CHECK, so a new
surface can start writing its own value the day it exists. Every read of it was
a two-way branch — `web`, or else Slack — in four places: the admin DM, the
audit feed, and the queue chip twice. An IdP-origin request would therefore
have been shown to admins as *Slack*, on the one field whose entire job is to
say where the request came from.

Worse than the label: result delivery asked the same question as `!= "web"`, so
a request from a surface that is not Slack would have had its CSV DM'd into
Slack anyway.
"""
import pathlib

import pytest

from queryhub import origins


# --- the vocabulary ---------------------------------------------------------

def test_the_two_live_values_read_as_themselves():
    assert origins.label("slack") == "Slack"
    assert origins.label("web") == "Web"


def test_the_idp_value_is_named_before_anything_writes_it():
    # PLA-479 goes through the same /api/* endpoints as the web UI, so it
    # inherits origin='web' unless it says otherwise. Naming the spelling here
    # is what stops the two sides inventing two of them.
    assert origins.IDP == "idp"
    assert origins.label("idp") == "IdP"


def test_an_unknown_origin_never_renders_as_slack():
    # The whole point. A surface nobody taught this module about shows as
    # itself — visibly unfamiliar — rather than as one of the first two.
    assert origins.label("cli") == "cli"
    assert origins.label("IDP") == "IdP"          # case-folded


def test_an_empty_origin_falls_back_to_the_column_default():
    assert origins.label(None) == "Slack"
    assert origins.label("") == "Slack"
    assert origins.label("   ") == "Slack"


# --- the behaviour that rode on the label -----------------------------------

def test_only_slack_counts_as_slacks_own_channel():
    assert origins.is_slack("slack") is True
    assert origins.is_slack(None) is True         # the default
    assert origins.is_slack("web") is False
    assert origins.is_slack("idp") is False       # was True under `!= "web"`


def test_result_delivery_asks_the_question_through_the_helper():
    """`_deliver_result_to_requester` used `!= "web"`, which reads identically
    for today's two values and stops being true at the third: an IdP request
    would have had its result DM'd to a Slack account its requester may not be
    using — and, after phase 3, may not have."""
    from queryhub import executor
    import inspect
    src = inspect.getsource(executor._deliver_result_to_requester)
    assert "origins.is_slack" in src
    assert '== "web"' not in src


# --- every rendering site goes through one place ----------------------------

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("rel", [
    "src/queryhub/slack_app/notifications.py",
    "src/queryhub/web/mapping.py",
])
def test_no_surface_hardcodes_the_two_way_branch(rel):
    # Code only: mapping.py's comments quote the old strings to explain why the
    # sign-in vocabulary is kept apart from this one, and a scan that counts
    # those fails for saying something true.
    lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
    code = "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))
    assert 'if origin == "web" else' not in code
    assert '"via web"' not in code


def test_the_queue_chip_renders_the_value_it_was_given():
    # Design-owned file, so this pins the contract rather than the markup: the
    # chip must not coerce an unknown origin into one of two names.
    src = (ROOT / "QueryHubWeb" / "qh-admin.jsx").read_text(encoding="utf-8")
    assert "qhOriginKey(it.origin)" in src
    assert "it.origin === 'web' ? 'web' : 'slack'" not in src
