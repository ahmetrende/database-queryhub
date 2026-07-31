"""The database-browse view, and the endpoint field it asked for.

The design added a second way to read the connection list: **Databases** — every
database the caller can reach in one flat alphabetical list, because that is how
developers name a target ("run it on billing_service", not "on the host it
happens to live on").

Two things here are worth a test rather than a comment.

The first is the shared renderer. Both views render database subtrees through one
`dbNode(...)`, so lazy schema loading, drag-to-insert, the right-click menu and
search-reveal behave identically in both. Two copies would drift, and the drift
would be silent: one view would quietly stop loading columns.

The second is the endpoint. The design asked for `host`/`port` on
`GET /connections` so two same-named databases can be told apart and the endpoint
pasted into a ticket. Told-apart is already solved without it (a duplicated name
carries its server name), so the field is served to ADMINS ONLY — a hostname is
infrastructure detail, and a requester does not need the fleet's endpoints to
write a query. This test exists because that gate is one `if` that a future
refactor of the payload could drop without anything looking wrong.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "QueryHubWeb"


def _read(name):
    return (WEB / name).read_text(encoding="utf-8")


def _routes_data():
    return (ROOT / "src" / "queryhub" / "web"
            / "routes_data.py").read_text(encoding="utf-8")


def test_endpoint_fields_are_admin_gated():
    """The whole point: `host`/`port` must sit under an admin check, not in the
    payload every whitelisted requester receives."""
    src = _routes_data()
    m = re.search(r"if is_admin:\s*\n\s*entry\[\"host\"\]", src)
    assert m, ("host/port must be assigned under `if is_admin:` — if the "
               "payload was refactored, re-gate it before shipping")
    # and they must not ALSO be set unconditionally somewhere above
    unconditional = re.search(r"^\s*(entry|out\.append\(\{[^}]*)\"host\"",
                              src, re.MULTILINE)
    assert unconditional is None or unconditional.start() > m.start(), \
        "host is set outside the admin branch"


def test_a_requester_payload_has_no_endpoint_and_the_ui_survives_that():
    """The view must degrade, not break, when host is absent — that is what
    makes the admin gate affordable."""
    panels = _read("qh-panels.jsx")
    assert "c.host || c.name" in panels, (
        "the hover must fall back to the server name; without the fallback the "
        "admin gate would leave non-admins with an 'undefined' tooltip")


def test_both_views_share_one_database_renderer():
    panels = _read("qh-panels.jsx")
    assert "const dbNode = (c, db, base" in panels, "the shared renderer is gone"
    # the server tree renders databases through it at depth 1
    assert "c.databases.map(db => dbNode(c, db, 1))" in panels
    # the flat list renders through it at depth 0
    assert "dbNode(row.c, row.db, 0" in panels
    # no second copy of the leaf structure
    assert panels.count("label=\"Tables\"") == 1, \
        "a second Tables node means the subtree was duplicated, not shared"


def test_the_flat_list_keeps_databases_of_the_same_name_apart():
    """Collapsing two same-named databases from different servers into one row
    would be a lie about the fleet."""
    panels = _read("qh-panels.jsx")
    assert "dupDbNames" in panels
    assert "qh-db-srv" in panels, "the disambiguating server name has no marker"


def test_database_favorites_are_keyed_per_connection():
    """`analytics` on one server is not `analytics` on another; a bare database
    name as the key would favourite both at once."""
    panels = _read("qh-panels.jsx")
    assert "const dbKey = (c, db) => c.id + '/' + db.id" in panels
    assert "dbFavorites" in panels and "dbFolders" in panels


def test_the_two_drag_payloads_do_not_collide():
    panels = _read("qh-panels.jsx")
    assert "application/x-qh-conn" in panels
    assert "application/x-qh-db" in panels, \
        "database rows need their own payload or dropping one files the other"


def test_the_organizer_is_cleared_on_sign_out_but_the_view_choice_is_not():
    """The organizer now files individual DATABASES, so on a shared
    machine it would tell the next person which databases the previous one
    worked in. The servers-vs-databases toggle is a preference, not user data."""
    app = _read("qh-app.jsx")
    assert "QH_CONNORG_KEY = 'qh.connorg.v1'" in app
    m = re.search(r"\[QH_WS_KEY,[^\]]*\]\.forEach", app, re.S)
    assert m, "the sign-out clear list moved; re-check what it covers"
    assert "QH_CONNORG_KEY" in m.group(0), \
        "the connection/database organizer is not cleared on sign-out"
    assert "qh.treeview.v1" not in m.group(0), \
        "the view toggle should survive sign-out; clearing it is just annoying"
