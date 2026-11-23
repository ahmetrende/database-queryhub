"""scripts/check_changelog_style.py: the What's-new writing rules, checked.

Entries use the version 2 shape: audience user or admin, a short title, a
tl;dr summary, bullets as a result line plus an optional detail, an action
line on a breaking entry, alt text on every screenshot, plain text only, none
of the forbidden phrases, and the same bullets in both languages.
"""
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_changelog_style.py"
_spec = importlib.util.spec_from_file_location("check_changelog_style", _PATH)
style = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(style)


def _entry(**over):
    e = {"id": "x", "d": "2026-10-05", "c": "query", "a": "user",
         "en": {"t": "Undo works for every edit", "s": "Press Cmd+Z to take back any change.",
                "p": [{"k": "fixed", "h": "You can undo a star expansion",
                       "d": "Press Cmd+Z after Tab."}]},
         "tr": {"t": "Undo works (tr)", "s": "Press Cmd+Z to undo (tr).",
                "p": [{"k": "fixed", "h": "Star expansion undoes (tr)"}]}}
    for k, v in over.items():
        e[k] = v
    return e


def test_a_good_entry_passes():
    assert style.check_entry(_entry()) == []


def test_word_limits_count_what_the_reader_sees():
    assert style._words("**Bold** and `code` count as words.") == 6
    e = _entry()
    e["en"]["p"][0]["h"] = " ".join(["word"] * 10)
    assert any("bullet 1 result line has 10 words (max 9)" in p for p in style.check_entry(e))
    e = _entry()
    e["en"]["p"][0]["d"] = " ".join(["word"] * 21)
    assert any("bullet 1 detail has 21 words" in p for p in style.check_entry(e))
    e = _entry()
    e["en"]["s"] = " ".join(["word"] * 21)
    assert any("summary has 21 words (max 20)" in p for p in style.check_entry(e))
    e = _entry()
    e["tr"]["t"] = " ".join(["kelime"] * 9)
    assert any("tr title has 9 words (max 8)" in p for p in style.check_entry(e))


def test_more_than_five_bullets_is_flagged():
    e = _entry()
    e["en"]["p"] = [{"k": "new", "h": "One"}] * 6
    e["tr"]["p"] = [{"k": "new", "h": "Bir"}] * 6
    assert any("6 bullets" in p for p in style.check_entry(e))


def test_the_old_bullet_shape_and_markup_are_refused():
    e = _entry()
    e["en"]["p"] = [{"k": "fixed", "t": "**Lead.** rest"}]
    assert any("old shape (t)" in p for p in style.check_entry(e))
    for text, name in (("Two **bold** words", '"**"'), ("Run `x` now", "a backtick"),
                       ("Fast — and safe", "an em dash"), ("A – B", "an en dash")):
        e = _entry()
        e["en"]["s"] = text
        assert any(f"en summary contains {name}" in p for p in style.check_entry(e)), text


def test_audience_kind_and_date_are_checked():
    hits = style.check_entry(_entry(a="approver"))
    assert any("audience must be user or admin" in p for p in hits)
    e = _entry()
    e["en"]["p"][0]["k"] = "added"
    e["tr"]["p"][0]["k"] = "added"
    assert any("kind must be one of" in p for p in style.check_entry(e))
    assert any("date must be YYYY-MM-DD" in p for p in style.check_entry(_entry(d="5 Oct")))


def test_a_breaking_entry_needs_an_action_line_in_every_language():
    e = _entry(brk=True)
    hits = style.check_entry(e)
    assert any("en has brk but no action line" in p for p in hits)
    assert any("tr has brk but no action line" in p for p in hits)
    e["en"]["act"] = "Open What's new from the portal."
    e["tr"]["act"] = "Open it from the portal (tr)."
    assert style.check_entry(e) == []
    e["en"]["act"] = " ".join(["word"] * 15)
    assert any("en action has 15 words (max 14)" in p for p in style.check_entry(e))
    e = _entry()
    e["en"]["act"] = "Do this."
    assert any("act) but the entry has no brk" in p for p in style.check_entry(e))


def test_screenshots_need_a_file_and_alt_text(tmp_path):
    (tmp_path / "x-after.png").write_bytes(b"png")
    e = _entry(img={"after": "x-after.png"})
    hits = style.check_entry(e, media_dir=tmp_path)
    assert any("en has no alt text for the after image" in p for p in hits)
    e["en"]["alt"] = {"after": "The new page with filters."}
    e["tr"]["alt"] = {"after": "Filtreli yeni sayfa."}
    assert style.check_entry(e, media_dir=tmp_path) == []
    e["img"] = {"before": "x-before.png", "after": "x-after.png"}
    hits = style.check_entry(e, media_dir=tmp_path)
    assert any('img.before "x-before.png" is not in docs/whatsnew' in p for p in hits)
    e["img"] = {"before": "x-after.png"}
    assert any("before image with no after image" in p
               for p in style.check_entry(e, media_dir=tmp_path))
    e["img"] = {"after": "../secret.png"}
    assert any("not a plain lower-case image file name" in p
               for p in style.check_entry(e, media_dir=tmp_path))


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
    e["tr"]["p"] = [{"k": "new", "h": "Yeni"}]
    assert any("en and tr bullets differ" in p for p in style.check_entry(e))


def test_ids_are_unique_and_categories_known():
    found = style.check([_entry(), _entry()], categories=["query"])
    assert found["x"][0] == "the id is not unique"
    found = style.check([_entry(c="nope")], categories=["query"])
    assert any("category 'nope'" in p for p in found["x"])


def test_strict_exits_1_and_default_warns(tmp_path, capsys):
    bad = _entry()
    bad["en"]["s"] = "Perhaps."
    f = tmp_path / "changelog.json"
    f.write_text(json.dumps({"categories": ["query"], "entries": [bad],
                             "style": {"forbidden": {"tr": ["hedgeword"]}}}), encoding="utf-8")
    assert style.main(["--file", str(f)]) == 0
    assert style.main(["--file", str(f), "--strict"]) == 1
    assert "1 with problems" in capsys.readouterr().out
    assert style.main(["--file", str(tmp_path / "missing.json"), "--strict"]) == 0


def test_english_text_meets_the_hard_ste_rules():
    if style._ste_lint() is None:
        pytest.skip("the asd-ste100 skill is not in this tree")
    e = _entry()
    e["en"]["s"] = "Tab expands the star; undo takes it back."
    assert any("breaks STE semicolon" in p for p in style.check_entry(e))
    e = _entry()
    e["en"]["s"] = "Tab expands the star. Undo takes it back."
    assert style.check_entry(e) == []
    e = _entry()
    e["en"]["p"][0]["d"] = "Press Tab; then undo."
    assert any("en bullet 1 detail breaks STE semicolon" in p for p in style.check_entry(e))
    e = _entry()
    e["tr"]["s"] = "Tab expands; undo restores (tr)."   # STE is English only
    assert not any("STE" in p for p in style.check_entry(e))
