"""scripts/check_changelog_style.py: the What's-new writing rules, checked.

Entries follow the i-have-adhd skill plus the limits in CLAUDE.md: short
title, summary of at most 25 words, at most five bullets of at most 20 words,
none of the forbidden phrases, and the same bullets in both languages.
"""
import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_changelog_style.py"
_spec = importlib.util.spec_from_file_location("check_changelog_style", _PATH)
style = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(style)


def _entry(**over):
    e = {"id": "x", "d": "2026-10-05", "c": "query", "a": "requester",
         "en": {"t": "Undo works for every edit", "s": "Press Cmd+Z to take back any change.",
                "p": [{"k": "fixed", "t": "**Expanding `*` can be undone.**"}]},
         "tr": {"t": "Undo works (tr)", "s": "Press Cmd+Z to undo (tr).",
                "p": [{"k": "fixed", "t": "**Star expansion undoes (tr).**"}]}}
    for k, v in over.items():
        e[k] = v
    return e


def test_a_good_entry_passes():
    assert style.check_entry(_entry()) == []


def test_word_limits_count_what_the_reader_sees():
    assert style._words("**Bold** and `code` count as words.") == 6
    long = " ".join(["word"] * 21)
    e = _entry()
    e["en"]["p"][0]["t"] = long
    assert any("bullet 1 has 21 words" in p for p in style.check_entry(e))
    e = _entry()
    e["en"]["s"] = " ".join(["word"] * 26)
    assert any("summary has 26 words" in p for p in style.check_entry(e))


def test_more_than_five_bullets_is_flagged():
    e = _entry()
    e["en"]["p"] = [{"k": "new", "t": "**One.**"}] * 6
    e["tr"]["p"] = [{"k": "new", "t": "**Bir.**"}] * 6
    assert any("6 bullets" in p for p in style.check_entry(e))


def test_forbidden_phrases_are_whole_words():
    e = _entry()
    e["en"]["s"] = "This might help, and we're excited."
    hits = style.check_entry(e)
    assert any('"might"' in p for p in hits) and any("we're excited" in p for p in hits)
    e = _entry()
    e["en"]["s"] = "The almighty mightiness of Tab."      # not the word itself
    assert style.check_entry(e) == []
    e = _entry()
    e["tr"]["s"] = "Hedgeword here."     # the Turkish list comes from the file
    assert any('"hedgeword"' in p for p in style.check_entry(e, {"tr": ["hedgeword"]}))


def test_both_languages_carry_the_same_bullets():
    e = _entry()
    e["tr"]["p"] = [{"k": "new", "t": "**Yeni.**"}]
    assert any("en and tr bullets differ" in p for p in style.check_entry(e))


def test_strict_exits_1_and_default_warns(tmp_path, capsys):
    bad = _entry()
    bad["en"]["s"] = "Perhaps."
    f = tmp_path / "changelog.json"
    f.write_text(json.dumps({"categories": [], "entries": [bad],
                             "style": {"forbidden": {"tr": ["hedgeword"]}}}), encoding="utf-8")
    assert style.main(["--file", str(f)]) == 0
    assert style.main(["--file", str(f), "--strict"]) == 1
    assert "1 with problems" in capsys.readouterr().out
    assert style.main(["--file", str(tmp_path / "missing.json"), "--strict"]) == 0


def test_english_text_meets_the_hard_ste_rules():
    if style._ste_lint() is None:
        import pytest
        pytest.skip("the asd-ste100 skill is not in this tree")
    e = _entry()
    e["en"]["s"] = "Tab expands the star; undo takes it back."
    assert any("breaks STE semicolon" in p for p in style.check_entry(e))
    e = _entry()
    e["en"]["s"] = "Tab expands the star. Undo takes it back."
    assert style.check_entry(e) == []
    e = _entry()
    e["tr"]["s"] = "Tab expands; undo restores (tr)."   # STE is English only
    assert not any("STE" in p for p in style.check_entry(e))
