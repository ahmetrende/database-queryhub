"""The Home cards say "loading" while their first load is pending.

History, saved queries, saved sessions and scheduled runs load after sign-in.
Until they settle the lists are empty, and the cards showed "0" and "Your recent
queries show here." That reads as "you have no history". These tests read the UI
files as a contract: the app owns the flag, the cards show a placeholder only
while a list is empty and loading, and the flag resets on sign-out and on retry.
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "QueryHubWeb"
APP = (WEB / "qh-app.jsx").read_text(encoding="utf-8")
HOME = (WEB / "qh-home.jsx").read_text(encoding="utf-8")
HTML = (WEB / "QueryHub.html").read_text(encoding="utf-8")


def test_the_app_owns_a_loading_flag_that_starts_true():
    assert re.search(r"const \[devLoading, setDevLoading\] = useState\(true\)", APP)


def test_the_flag_ends_when_the_whole_load_settles_even_on_failure():
    """`allSettled`, not `all`: one failed call must not leave the cards loading
    forever. The failure is reported by the existing retry banner."""
    block = APP.split("Promise.allSettled(jobs).then(res => {", 1)[1].split("});", 1)[0]
    assert "setDevLoading(false)" in block


def test_a_retry_and_a_sign_out_start_loading_again():
    reload = APP.split("const reloadDev = useCallback(", 1)[1].split("}, [user]);", 1)[0]
    assert "setDevLoading(true)" in reload
    signout = APP.split("const signOut = () => {", 1)[1].split("const wsInit", 1)[0]
    assert "setDevLoading(true)" in signout


def test_the_home_screen_receives_the_flag():
    assert re.search(r"<HomeScreen user=\{user\} loading=\{devLoading\}", APP)
    assert re.search(r"function HomeScreen\(\{ user, loading,", HOME)


def test_each_loaded_list_shows_a_placeholder_instead_of_zero():
    """Four cards read server data. 'Current session' is local and never waits."""
    assert "const wait = (list) => !!loading && (list || []).length === 0;" in HOME
    for title, lst in (("Saved sessions", "sessions"), ("Recent history", "history"),
                       ("Saved queries", "saved"), ("Scheduled", "scheduled || []")):
        card = HOME.split(f'<HomeCard title="{title}"', 1)[1].split("</HomeCard>", 1)[0]
        assert f"count={{wait({lst}) ? null :" in card, title
        assert f"{{wait({lst}) ? <HomePlaceholder />" in card, title
    current = HOME.split('<HomeCard title="Current session"', 1)[1].split("</HomeCard>", 1)[0]
    assert "wait(" not in current


def test_the_placeholder_is_announced_once_and_respects_reduced_motion():
    comp = HOME.split("function HomePlaceholder()", 1)[1].split("function HomeEmpty", 1)[0]
    assert 'role="status"' in comp and '<span className="qh-skel-sr">Loading</span>' in comp
    assert comp.count('aria-hidden="true"') >= 1
    css = HTML.split(".qh-skel {", 1)[1].split(".qh-treeview {", 1)[0]
    assert "prefers-reduced-motion: reduce" in css and "animation: none" in css
