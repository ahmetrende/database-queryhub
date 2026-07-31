"""Every colour variable set under `:root` must be restored for dark mode.

The trap, found in the result tabs: the accessibility pass raised the light
`--fg-tertiary` from 0.60 to 0.66 and wrote it under

    :root, [data-theme='light'] { --fg-tertiary: rgba(31,34,41,0.66); }

`:root` matches in DARK mode too. `:root` (a pseudo-class) and `[data-theme]` (an
attribute selector) have EQUAL specificity, so source order decides — and this
file loads after the vendored theme that defines the correct dark value. Dark
mode was painting dark ink on a dark background, and every element using
`--fg-tertiary` disappeared: timestamps, tier sublabels, column metadata, and
most visibly the inactive result tabs (Plan / Messages / Audit log).

The dark block even carried a comment saying dark "already passes, so it is
deliberately left alone" — true of the vendor value, false of the value that
actually applied. That is the shape worth guarding: the reasoning was about a
variable the file was itself overwriting.

So this test does not check one variable. It parses the theme blocks and asserts
the invariant, which means the next variable added under `:root` is covered the
day it is added rather than when somebody notices text has gone missing.
"""
import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parent.parent / "QueryHubWeb" / "QueryHub.html"

# Variables whose value is a colour and therefore theme-dependent. A layout or
# sizing variable set once for both themes is fine and must not be flagged.
_COLOUR_RE = re.compile(r"(rgba?\(|#[0-9a-fA-F]{3,8}\b|\bhsla?\()")


def _block(selector: str) -> str:
    """The body of the first rule whose selector list contains `selector`."""
    text = CSS.read_text(encoding="utf-8")
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        if selector in m.group(1) and "--" in m.group(2):
            return m.group(2)
    raise AssertionError(f"no variable block found for {selector!r}")


def _vars(body: str) -> dict[str, str]:
    return {name: value.strip() for name, value in
            re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)}


@pytest.fixture(scope="module")
def blocks():
    root = _vars(_block(":root, [data-theme='light']"))
    dark = _vars(_block("[data-theme='dark']"))
    assert root, "light/root block parsed empty — the parser is wrong, not the CSS"
    assert dark, "dark block parsed empty"
    return root, dark


def test_the_parser_actually_found_the_known_variables(blocks):
    """Guard the guard: a regex that matches nothing would make this file pass
    no matter what the CSS said."""
    root, dark = blocks
    assert "--fg-tertiary" in root
    assert "--tk-com" in root and "--tk-com" in dark


def test_every_colour_var_set_under_root_is_overridden_for_dark(blocks):
    """The invariant. `:root` leaks into dark mode, so a colour set there and not
    restored below is a light-mode value applied to a dark background."""
    root, dark = blocks
    leaked = [name for name, value in root.items()
              if _COLOUR_RE.search(value) and name not in dark]
    assert not leaked, (
        "these colour variables are set under `:root` (which matches in dark "
        "mode) and never restored in the [data-theme='dark'] block, so dark "
        f"mode gets the light value: {leaked}. Either move the declaration into "
        f"[data-theme='light'] only, or add a dark value.")


def test_the_dark_fg_tertiary_is_a_light_ink(blocks):
    """The specific regression, pinned by direction rather than exact value: on a
    dark background the ink must be light. A future tweak may change 0.50, but it
    must not go back to a dark colour."""
    _, dark = blocks
    val = dark["--fg-tertiary"]
    assert "255,255,255" in val.replace(" ", ""), \
        f"dark --fg-tertiary is not a light colour: {val}"


def test_the_check_would_catch_a_reintroduced_leak(blocks):
    """Mutation check on the assertion itself, so it cannot be vacuous."""
    root, dark = blocks
    pretend_root = dict(root, **{"--fg-quaternary": "rgba(31,34,41,0.5)"})
    leaked = [n for n, v in pretend_root.items()
              if _COLOUR_RE.search(v) and n not in dark]
    assert leaked == ["--fg-quaternary"]


def test_a_non_colour_variable_is_not_flagged():
    """Sizing and font variables are theme-independent; flagging them would make
    the test noise that gets suppressed."""
    assert not _COLOUR_RE.search("ui-monospace, 'SF Mono', Menlo, monospace")
    assert not _COLOUR_RE.search("52px")
    assert _COLOUR_RE.search("rgba(31,34,41,0.66)")
    assert _COLOUR_RE.search("#16181D")
