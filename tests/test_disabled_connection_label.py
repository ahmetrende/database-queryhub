"""A disabled connection must be visibly disabled, and env tags stay hidden.

Both come from the same afternoon. A target was disabled because its data had
moved to another cloud — and it kept appearing in the admin's picker looking
exactly like the live one. That is not a display bug: a retired instance still answers,
so picking it returns stale data with no error anywhere. The label is the only
thing standing between habit and a wrong answer.

Why disabled targets appear for an admin at all (deliberate, see
routes_data.connections): their saved queries and history can reference a
disabled alias, and without the target in the list those tabs cannot resolve it.
So the fix is to LABEL, not to hide.

Env tags are the opposite call: the fleet is prod-first, so PROD sat on nearly
every row and carried no information there. Hidden behind a flag rather than
deleted, because "keep it in case we need it later" was the ask and a flag is
the version of that which cannot rot. The TOOLBAR keeps its tag — that line is
what you read just before pressing Run.
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "QueryHubWeb"


def _read(name):
    return (WEB / name).read_text(encoding="utf-8")


def test_the_api_tells_the_ui_a_connection_is_disabled():
    """The label is impossible without this field, and it is easy to drop when
    the connections payload is next refactored."""
    src = (Path(__file__).resolve().parent.parent / "src" / "dba_slack_bot"
           / "web" / "routes_data.py").read_text(encoding="utf-8")
    assert '"disabled": not t.enabled' in src


def test_every_place_that_shows_a_connection_shows_when_it_is_disabled():
    """FOUR surfaces now: the server tree, the flat database list, the omnibox
    suggestions, and the toolbar. Missing one is how the accident comes back
    through the other door — which is why this asserts each surface by its own
    expression instead of counting occurrences. A count breaks (and gets bumped
    without thought) the moment a fifth surface appears; naming them means a new
    surface that forgets the marker still fails."""
    panels, app = _read("qh-panels.jsx"), _read("qh-app.jsx")
    # server tree row + flat database row (row.c is the connection behind a db)
    assert "c.disabled" in panels, "the server tree row"
    assert "row.c.disabled" in panels, "the flat database row"
    assert "m.c.disabled" in panels, "the omnibox suggestion"
    assert "conn.disabled" in app, "the toolbar — the last line before Run"
    # and each of those three in panels must actually render the marker
    assert panels.count("qh-conn-off") >= 3, \
        "a surface names .disabled but does not render the marker"
    assert "qh-conn-off" in app


def test_the_marker_has_a_style_that_reads_as_a_warning():
    css = _read("QueryHub.html")
    m = re.search(r"\.qh-conn-off \{([^}]*)\}", css)
    assert m, "no .qh-conn-off rule — the label would render unstyled"
    body = m.group(1)
    assert "warning" in body, ("it must not look like a neutral chip; the point "
                              "is to interrupt a habit")


def test_env_tags_are_hidden_in_the_list_but_kept_in_the_code():
    data, panels = _read("qh-data.jsx"), _read("qh-panels.jsx")
    assert "const QH_SHOW_ENV_TAGS = false;" in data, \
        "the flag is the whole 'hidden, not deleted' contract"
    assert "QH_SHOW_ENV_TAGS," in data.split("Object.assign(window,")[-1], \
        "not exported to window, so the other files cannot read it"
    # every env tag in the LIST is behind the flag
    for m in re.finditer(r"qh-envtag-sm env-' \+ ([a-z.]+)\.env", panels):
        start = max(0, m.start() - 400)
        assert "QH_SHOW_ENV_TAGS" in panels[start:m.start()], \
            f"an env tag in the list is not behind the flag: {m.group(0)}"


def test_the_toolbar_still_shows_the_environment():
    """Deliberate asymmetry — do not 'tidy' this into consistency with the list."""
    app = _read("qh-app.jsx")
    m = re.search(r"qh-envtag-sm env-' \+ conn\.env", app)
    assert m, "the toolbar env tag was removed; it is the one that matters"
    assert "QH_SHOW_ENV_TAGS" not in app[max(0, m.start() - 300):m.start()], \
        "the toolbar tag must not be gated by the list flag"


def test_flipping_the_flag_is_all_it_takes_to_bring_them_back():
    """Guard the guard: if the flag were referenced nowhere, setting it true
    would silently do nothing and the 'keep it for later' promise would be a lie."""
    assert _read("qh-panels.jsx").count("QH_SHOW_ENV_TAGS") == 2
