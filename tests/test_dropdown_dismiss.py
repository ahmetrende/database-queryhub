"""Menus must not close because the pointer left them.

Both dropdowns in the app were written as

    <div className="qh-export" onMouseLeave={() => setExp(false)}>

with the popup positioned 6px below a 28px button (`top: 34px`, and
`r.bottom + 6` for the shortcuts one). Those 6px belong to neither element, so
moving the pointer from the button down to the menu left the wrapper, fired
mouseleave, and the menu vanished before it could be clicked. It was reported as
"the export menu disappears when I move the mouse to it".

The rule is also just wrong for a menu opened by a CLICK: it should stay until
dismissed, so a slow or imprecise movement cannot lose it.

These components are refreshed wholesale rather than patched line by line, so
the pattern can come back — the fix would be silently undone and only noticed
by a user again. Hence a test rather than a comment.
"""
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "QueryHubWeb"
# The app's own sources only. Reference copies that live in subdirectories are
# documentation, not what gets built, so the glob deliberately does not recurse.
SOURCES = sorted(WEB.glob("qh-*.jsx"))

# `onMouseLeave` itself is fine — a hover tooltip or a row highlight is a
# legitimate use. What must not come back is using it to CLOSE something.
_DISMISS_ON_LEAVE = re.compile(
    r"onMouseLeave\s*=\s*\{[^}]*set[A-Za-z]*\(\s*(?:false|null)\s*\)")

# Comments are stripped first. Without that, this file's own explanation of the
# bug — which quotes the offending line — trips the check, and the honest fix is
# for the scan to read code rather than prose. (Caught by that happening.)
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _code(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", src))


def test_there_are_sources_to_check():
    """Guard the guard: a glob that matched nothing would pass silently."""
    assert len(SOURCES) >= 5, [p.name for p in SOURCES]


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_menu_closes_itself_on_mouse_leave(path):
    hits = _DISMISS_ON_LEAVE.findall(_code(path))
    assert not hits, (
        f"{path.name} dismisses a popup on mouseleave: {hits}. A gap between the "
        "trigger and the popup makes that unreachable — use qhUseDismiss "
        "(click-away + Escape) instead.")


def test_the_comment_stripper_does_not_hide_real_code():
    """Guard the guard the other way: stripping comments must not swallow a live
    line, or the check becomes decorative."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "x.jsx"
        f.write_text('// onMouseLeave={() => setX(false)}\n'
                     'const a = 1; // trailing\n'
                     '<div onMouseLeave={() => setY(false)}>\n')
        hits = _DISMISS_ON_LEAVE.findall(_code(f))
        assert len(hits) == 1, hits          # the commented-out one is ignored
        assert "setY" in hits[0]
        assert "const a = 1;" in _code(f)    # code on a line with a trailing
                                             # comment survives


def test_the_shared_dismiss_hook_exists_and_is_exported():
    """It lives in qh-modal.jsx because that file loads before both menus
    (see app/src/main.jsx) and these files share globals, not imports."""
    src = (WEB / "qh-modal.jsx").read_text(encoding="utf-8")
    assert "function qhUseDismiss(" in src
    assert "qhUseDismiss" in src.split("Object.assign(window,")[-1], \
        "the hook is not on window, so the other files cannot see it"
    # The two behaviours that replace mouseleave.
    assert "pointerdown" in src
    assert "'Escape'" in src


def test_the_hook_is_loaded_before_its_users():
    """Global scope, so order is the only thing making it defined."""
    main = (WEB / "app" / "src" / "main.jsx").read_text(encoding="utf-8")
    order = [ln for ln in main.splitlines() if "import './qh-" in ln]
    idx = {name: i for i, ln in enumerate(order)
           for name in ("qh-modal.jsx", "qh-editor.jsx", "qh-panels.jsx")
           if name in ln}
    assert idx["qh-modal.jsx"] < idx["qh-editor.jsx"]
    assert idx["qh-modal.jsx"] < idx["qh-panels.jsx"]


@pytest.mark.parametrize("path,wrapper", [
    ("qh-panels.jsx", "qh-export"),
    ("qh-editor.jsx", "qh-kb-wrap"),
])
def test_both_menus_use_the_hook(path, wrapper):
    src = (WEB / path).read_text(encoding="utf-8")
    m = re.search(rf'className="{wrapper}"([^>]*)>', src)
    assert m, f"{wrapper} not found in {path}"
    assert "ref={" in m.group(1), \
        f"{wrapper} has no dismiss ref attached: {m.group(1)!r}"
    assert "qhUseDismiss(" in src
