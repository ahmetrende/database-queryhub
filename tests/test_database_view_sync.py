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


def test_the_endpoint_never_escapes_the_grant_filter():
    """`host`/`port` went from admin-only to everyone on 2026-08-15, and the
    thing that makes that safe is not a role check — it is that this loop is
    GRANT-FILTERED. `effective_grant_for_user` returns None and the target is
    skipped, so a payload can only ever carry the endpoint of a machine the
    caller already has standing access to.

    That ordering is the invariant now. If the grant check ever moves below the
    payload build, or the endpoint is assigned before it, every whitelisted
    requester starts receiving the whole fleet's addresses — which is exactly
    the outcome the old admin gate was reaching for."""
    src = _routes_data()
    grant = re.search(r"grant = teams\.effective_grant_for_user\(uid, t\.id\)", src)
    skip = re.search(r"if grant is None:\s*\n\s*continue", src)
    host = re.search(r"^\s*entry\[\"host\"\]", src, re.MULTILINE)
    assert grant and skip and host, "the connections loop was refactored — re-read it"
    assert grant.start() < skip.start() < host.start(), (
        "the endpoint is built before the ungranted target is skipped — a "
        "requester would receive addresses they hold no grant on")


def test_the_ui_drops_the_endpoint_when_there_is_none_rather_than_faking_one():
    """A target row can still carry no host (an unfilled registry row). The
    hover used to fall back to the server ALIAS in the hostname's place, which
    reads as an endpoint and is not one; and Copy endpoint would have copied
    it. Both are conditional on a real host now."""
    panels = _read("qh-panels.jsx")
    # The HOVER: the endpoint line is emitted only when there is a host. It used
    # to read `(c.host || c.name) + port`, which printed the alias where a
    # hostname belongs.
    assert "c.host ? c.host + (c.port ? ':' + c.port : '') : ''" in panels, (
        "the database-view hover is back to putting the alias in the endpoint "
        "position — that reads as a hostname and is not one")
    # The MENU: both Copy endpoint items are gated on a real host. `copyEndpoint`
    # keeps an internal `c.host || c.name` fallback, which is fine precisely
    # because these guards make it unreachable — so the guards are the thing to
    # pin, not the fallback's absence.
    assert "{menu.conn.host && " in panels and "{menu.c.host && " in panels, (
        "Copy endpoint must be hidden when the payload carries no host, or the "
        "alias fallback inside copyEndpoint becomes reachable")


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
