"""The sidebar says "loading" while /connections loads.

Before the first answer the list of servers is empty, and an empty tree reads as
"you have no servers". On a slow link that lasted seconds. These tests read the
UI files as a contract: the app owns the flag, the sidebar draws the skeleton,
and a failed load ends the loading state too.
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "QueryHubWeb"
APP = (WEB / "qh-app.jsx").read_text(encoding="utf-8")
PANELS = (WEB / "qh-panels.jsx").read_text(encoding="utf-8")
HTML = (WEB / "QueryHub.html").read_text(encoding="utf-8")


def test_the_app_owns_a_loading_flag_that_starts_true():
    assert re.search(r"const \[connsLoading, setConnsLoading\] = useState\(true\)", APP)


def test_the_flag_ends_on_success_and_on_failure():
    """`.finally`, not `.then`: a failed load must not leave the skeleton on
    forever. The failure itself is reported by the existing retry banner."""
    m = re.search(r"qhApi\.connections\(\)\.then\(r => setConns\([^)]*\)\)\.finally\(\(\) => setConnsLoading\(false\)\)", APP)
    assert m, "the first load no longer ends the loading state"


def test_a_retry_starts_loading_again():
    block = APP.split("const reloadDev = useCallback(", 1)[1].split("}, [user]);", 1)[0]
    assert "setConnsLoading(true)" in block


def test_the_sidebar_receives_the_flag_and_draws_the_skeleton_only_while_empty():
    assert "connsLoading={connsLoading}" in APP
    assert re.search(r"function Sidebar\(\{[^}]*\bconnsLoading\b", PANELS)
    assert "connsLoading && conns.length === 0 ? <QhConnSkeleton />" in PANELS


def test_the_skeleton_is_announced_once_and_respects_reduced_motion():
    comp = PANELS.split("function QhConnSkeleton()", 1)[1].split("function Sidebar(", 1)[0]
    assert 'role="status"' in comp
    # The announcement is a text node inside the region: a label on a region
    # whose only content is aria-hidden is not reliably read.
    assert '<span className="qh-skel-sr">Loading servers</span>' in comp
    assert comp.count('aria-hidden="true"') >= 1
    css = HTML.split(".qh-skel {", 1)[1].split(".qh-treeview {", 1)[0]
    assert "prefers-reduced-motion: reduce" in css
    assert "animation: none" in css


def test_sign_out_forgets_the_server_tree():
    """Sign-out does not reload the page, and the tree is the previous user's."""
    block = APP.split("const signOut = () => {", 1)[1].split("const wsInit", 1)[0]
    assert "setConns([])" in block and "setConnsLoading(true)" in block
