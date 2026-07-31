"""Frontend accessibility + design-scaffolding invariants, checked at the source.

There is no browser in this suite, so these are structural: they pin the things
that were broken and are easy to silently undo when these files are
refreshed wholesale — every modal
going through the shared dialog shell, the queue card not nesting interactive
elements, the contrast-corrected token values, and the design-tool scaffolding
staying out of the production bundle.

WCAG ratios are recomputed here rather than trusted from a comment: the token
alphas are the fix, so if someone lowers one, this fails.
"""
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "QueryHubWeb"
# Top-level sources only. Copies in subdirectories are reference material,
# not what gets built.
LIVE_JSX = [p for p in WEB.glob("*.jsx")]


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- modals


def test_every_modal_uses_the_shared_dialog_shell():
    """A raw `qh-modal-overlay` div means a modal with no role, no focus trap
    and no Escape — the state all six were in."""
    offenders = []
    for path in LIVE_JSX:
        if path.name == "qh-modal.jsx":
            continue           # the shell itself owns the markup
        if "qh-modal-overlay" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == [], f"hand-rolled modal overlay in {offenders}"


def test_the_shell_provides_dialog_semantics_focus_and_escape():
    src = read("qh-modal.jsx")
    assert 'role="dialog"' in src
    assert 'aria-modal="true"' in src
    assert "aria-labelledby" in src
    assert "'Escape'" in src
    assert "'Tab'" in src
    # Focus has to come back to where it was, or closing a modal drops the
    # caret at the top of the document.
    assert "document.activeElement" in src
    assert "restoreRef" in src


def test_modal_shell_loads_before_its_consumers():
    """QhModal is read as a global by qh-panels/qh-app; if it loads after them
    the app throws on the first modal open."""
    main = (WEB / "app" / "src" / "main.jsx").read_text(encoding="utf-8")
    order = [m for m in re.findall(r"import '\./([\w.-]+)'", main)]
    assert order.index("qh-modal.jsx") < order.index("qh-panels.jsx")
    assert order.index("qh-modal.jsx") < order.index("qh-app.jsx")
    # Same for the raw prototype's <script> tags.
    html = read("QueryHub.html")
    assert html.index('src="qh-modal.jsx"') < html.index('src="qh-panels.jsx"')


# ---------------------------------------------------------------- queue card


def test_queue_card_does_not_nest_interactive_elements():
    """A <label>/<input> inside a <button> is invalid HTML with undefined AT
    behaviour, and it made the card's accessible name the whole SQL body."""
    src = read("qh-admin.jsx")
    card = src[src.index("function QueueCard"):]
    card = card[:card.index("\nfunction ")]
    assert "<label" in card and "<button" in card
    # The label must not be inside the button: find both and compare order
    # against the button's own closing tag.
    assert card.index("<label") < card.index("<button type=\"button\""), \
        "checkbox label must be a sibling before the card button"
    assert "aria-label=" in card


def test_result_grid_is_keyboard_navigable():
    src = read("qh-panels.jsx")
    for key in ("ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"):
        assert f"'{key}'" in src, f"grid has no {key} handling"
    assert 'role="grid"' in src
    assert "data-cell=" in src          # cursor scroll-into-view addressing


# ---------------------------------------------------------------- contrast


def _ratio(fg, alpha, bg):
    def lin(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def lum(p):
        return 0.2126 * lin(p[0]) + 0.7152 * lin(p[1]) + 0.0722 * lin(p[2])

    over = tuple(fg[i] * alpha + bg[i] * (1 - alpha) for i in range(3))
    a, b = lum(over), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _alpha(css, token, rgb_prefix):
    m = re.search(re.escape(token) + r":\s*rgba\(" + rgb_prefix + r",\s*([0-9.]+)\)", css)
    assert m, f"{token} ({rgb_prefix}) not found"
    return float(m.group(1))


# The surfaces each token lands on, per theme. Named by role, not by theme, so
# this file says nothing about which theme ships as the default.
DEFAULT_LIGHT_SURFACES = [(255, 255, 255), (250, 250, 249), (246, 246, 245)]
DEFAULT_DARK_SURFACES = [(0x18, 0x1A, 0x20), (0x1F, 0x22, 0x29), (0x23, 0x2A, 0x31)]
ALT_LIGHT_SURFACES = [(0xFD, 0xFC, 0xFA), (0xF6, 0xF4, 0xEE), (0xF3, 0xF0, 0xE8)]
ALT_DARK_SURFACES = [(0x26, 0x24, 0x1F), (0x2E, 0x2B, 0x25), (0x20, 0x1E, 0x19)]


@pytest.mark.parametrize("token", ["--fg-tertiary", "--tk-com"])
def test_light_text_tokens_meet_wcag_aa(token):
    css = read("QueryHub.html")
    alpha = _alpha(css, token, r"31,34,41")
    worst = min(_ratio((31, 34, 41), alpha, bg) for bg in DEFAULT_LIGHT_SURFACES)
    assert worst >= 4.5, f"{token} at {alpha} is {worst:.2f}:1 on the worst surface"


def test_dark_sql_comment_meets_wcag_aa():
    css = read("QueryHub.html")
    alpha = _alpha(css, "--tk-com", r"255,255,255")
    worst = min(_ratio((255, 255, 255), alpha, bg) for bg in DEFAULT_DARK_SURFACES)
    assert worst >= 4.5, f"--tk-com at {alpha} is {worst:.2f}:1"


def test_alternate_theme_text_tokens_meet_wcag_aa():
    """Found by glob rather than by filename: the alternate theme stylesheet is
    renamed on export, and a test that hardcodes one repo's name is a test that
    silently stops running in the other."""
    sheets = sorted(WEB.glob("*theme*.css"))
    assert sheets, "no theme stylesheet found"
    for sheet in sheets:
        css = sheet.read_text(encoding="utf-8")
        for token, fg, prefix, bgs in (
            ("--fg-tertiary", (27, 26, 23), r"27,26,23", ALT_LIGHT_SURFACES),
            ("--tk-com", (27, 26, 23), r"27,26,23", ALT_LIGHT_SURFACES),
            ("--fg-tertiary", (237, 231, 219), r"237,231,219", ALT_DARK_SURFACES),
            ("--tk-com", (237, 231, 219), r"237,231,219", ALT_DARK_SURFACES),
        ):
            alpha = _alpha(css, token, prefix)
            worst = min(_ratio(fg, alpha, bg) for bg in bgs)
            assert worst >= 4.5, \
                f"{sheet.name} {token} ({prefix}) at {alpha} is {worst:.2f}:1"


# ------------------------------------------------- design-tool scaffolding


def test_tweaks_panel_host_protocol_is_gated():
    """The postMessage listener has no origin check and the handshake posts to
    '*'. That belongs to the design host only — any page holding a window
    handle could otherwise reveal the panel in production."""
    src = read("tweaks-panel.jsx")
    listener = src.index("addEventListener('message'")
    gate = src.rindex("qhDesignMode()", 0, listener)
    assert listener - gate < 400, "message listener is not behind qhDesignMode()"
    assert "window.__QH_DESIGN_MODE__ === true" in src


def test_production_entry_never_enables_design_mode():
    app_html = (WEB / "app" / "index.html").read_text(encoding="utf-8")
    assert "__QH_DESIGN_MODE__" not in app_html
    # The prototype is the design host, so it does set it.
    assert "__QH_DESIGN_MODE__ = true" in read("QueryHub.html")


def test_dead_role_switch_is_gone():
    """A control labelled "Your role: developer / super" in a security tool,
    whose value nothing reads."""
    src = read("qh-app.jsx")
    assert "Access (demo)" not in src
    assert "'role'" not in src or "setTweak('role'" not in src


def test_login_takes_the_org_name_from_the_server():
    """The design mock hardcodes a company name; every install advertised it."""
    src = read("qh-login.jsx")
    assert "orgLabel" in src
    assert "qhBrand" not in src or "().org" not in src
