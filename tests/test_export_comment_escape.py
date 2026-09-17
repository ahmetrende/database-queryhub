"""A comment may say the letter; code may not have it changed underneath it.

The public tree is English-only and the exporter's brand scan enforces that by
refusing any Turkish letter. The 2026-09-17 design port landed a comment whose
whole point was a letter -- an ascii handle cannot recover its diacritic -- and
the rule had no way to let it through.

Folding it to ascii was the first answer and it was wrong in a way worth
keeping: both sides of "never" came out the same word, so the exported sentence
contradicted itself while passing every gate. Escaping says exactly what the
fold erased, and it only ever touches a comment: a string literal that changed
value on the way out would be a bug nobody could see from either repository.
"""
import importlib.util
import pathlib

import pytest

_PATH = (pathlib.Path(__file__).resolve().parents[1]
         / "scripts" / "export_independent_repo.py")
upstream_only = pytest.mark.skipif(
    not _PATH.exists(), reason="the exporter is upstream-only")


def _module():
    spec = importlib.util.spec_from_file_location("_export_repo", _PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:  # pragma: no cover - dependency missing in this env
        pytest.skip("the exporter needs its own imports")
    return mod


def _escape():
    return _module()._escape_turkish_in_comments


class _Jsx:
    suffix = ".jsx"


@upstream_only
def test_a_comment_keeps_the_distinction_the_fold_destroyed():
    esc = _escape()
    s_cedilla = chr(0x15E)      # the dotted-s spelling, as a codepoint: this
                                # file stays ascii like the rest of the repo
    line = f"// `Sahin`, never `{s_cedilla}ahin`; the letter cannot be invented.\n"
    out = esc(line, _Jsx())
    assert out == "// `Sahin`, never `\\u015eahin`; the letter cannot be invented.\n"
    assert s_cedilla not in out


@upstream_only
def test_code_is_left_for_the_scan_to_refuse():
    """Silently rewriting a literal would change what the published app does.
    The export stopping is the correct outcome there, and it still does."""
    esc = _escape()
    code = "const label = '" + chr(0x15E) + "ahin';\n"
    assert esc(code, _Jsx()) == code


@upstream_only
def test_every_letter_the_scan_refuses_has_an_escape():
    mod = _module()
    assert len(mod._TR_LETTERS) == 12
    for letter in mod._TR_LETTERS:
        assert letter not in mod._escape_turkish_in_comments(
            "// " + letter + "\n", _Jsx())


@upstream_only
def test_a_file_with_no_turkish_letter_is_returned_unchanged():
    """The sweep runs over every exported UI file; the normal case must not
    pay for the rare one, and must not be rewritten at all."""
    esc = _escape()
    text = "// nothing to do here\nconst a = 1;\n"
    assert esc(text, _Jsx()) is text
