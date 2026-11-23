"""web/changelog.py: the What's-new feed, version 2.

The feed reads both entry shapes, sends admin entries to admins only, serves
the requested language with an English fallback, and serves screenshots from
docs/whatsnew only.
"""
import json

import pytest

from queryhub.web import changelog


def _write(tmp_path, monkeypatch, data):
    f = tmp_path / "changelog.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(changelog, "_source_path", lambda: f)
    changelog._cache.update(v=None, mtime=None, path=None)
    return f


V2 = {
    "id": "grid", "d": "2026-11-22", "c": "web", "a": "user", "brk": True,
    "img": {"before": "grid-before.png", "after": "grid-after.png"},
    "en": {"t": "Every row opens", "s": "Page through every stored row.",
           "act": "Open results from the History tab.",
           "alt": {"before": "Old grid", "after": "New grid"},
           "p": [{"k": "fixed", "h": "Every stored row opens",
                  "d": "Results no longer stop at 1,000 rows."}]},
    "tr": {"t": "Every row opens (tr)", "s": "You can see every stored row (tr).",
           "act": "Open the results from History (tr).",
           "p": [{"k": "fixed", "h": "Every stored row opens (tr)"}]},
}
OLD = {
    "id": "queue", "d": "2026-11-20", "c": "access", "a": "approver",
    "en": {"t": "Approve comes first", "s": "The queue sorts by age.",
           "p": [{"k": "changed", "t": "**Oldest first.** The queue sorts by age."}]},
    "tr": {"t": "Approval first (tr)", "s": "The queue sorts by age (tr)."},
}
BOTH = {"id": "both", "d": "2026-11-19", "c": "query", "a": "both",
        "en": {"t": "Shared entry", "s": "Old audience value both."}}


@pytest.fixture
def feed_file(tmp_path, monkeypatch):
    return _write(tmp_path, monkeypatch, {
        "entries": [OLD, V2, BOTH],
        "i18n": {"tr": {"categories": {"web": "Web UI (tr)"},
                        "ui": {"title": "Yenilikler"}}},
    })


def test_admin_entries_reach_admins_only(feed_file):
    ids = [r["id"] for r in changelog.releases(for_admin=False)]
    assert ids == ["grid", "both"]
    ids = [r["id"] for r in changelog.releases(for_admin=True)]
    assert ids == ["grid", "queue", "both"]


def test_old_audience_values_map_to_user_or_admin(feed_file):
    by_id = {r["id"]: r for r in changelog.releases(for_admin=True)}
    assert by_id["queue"]["audience"] == "admin"
    assert by_id["both"]["audience"] == "user"


def test_a_v2_entry_has_plain_text_action_and_shots(feed_file):
    r = changelog.releases()[0]
    assert r["headline"] == "Every row opens"
    assert r["breaking"] is True
    assert r["action"] == "Open results from the History tab."
    assert r["changes"] == [{"type": "fixed", "text": "Every stored row opens",
                             "detail": "Results no longer stop at 1,000 rows."}]
    assert r["shots"] == {"before": "grid-before.png", "beforeAlt": "Old grid",
                          "after": "grid-after.png", "afterAlt": "New grid"}
    assert r["date"] == "2026-11-22" and r["area"] == "Web UI"


def test_an_old_bold_lead_becomes_the_result_line(feed_file):
    r = [x for x in changelog.releases(for_admin=True) if x["id"] == "queue"][0]
    assert r["changes"] == [{"type": "changed", "text": "Oldest first",
                             "detail": "The queue sorts by age."}]
    assert "**" not in json.dumps(changelog.releases(for_admin=True))


def test_turkish_text_with_english_fallback(feed_file):
    out = changelog.feed(for_admin=True, requested_lang="tr")
    assert out["lang"] == "tr" and out["languages"] == ["en", "tr"]
    assert out["ui"] == {"title": "Yenilikler"}
    by_id = {r["id"]: r for r in out["releases"]}
    assert by_id["grid"]["summary"] == "You can see every stored row (tr)."
    assert by_id["grid"]["area"] == "Web UI (tr)"
    # The alt text has no Turkish copy, so the English one is used.
    assert by_id["grid"]["shots"]["afterAlt"] == "New grid"
    # No Turkish text at all: the English entry is served.
    assert by_id["both"]["headline"] == "Shared entry"


def test_an_unknown_language_is_english(feed_file):
    out = changelog.feed(for_admin=False, requested_lang="de")
    assert out["lang"] == "en" and out["ui"] == {}
    assert out["isAdmin"] is False


def test_a_missing_file_is_an_empty_feed(tmp_path, monkeypatch):
    monkeypatch.setattr(changelog, "_source_path", lambda: tmp_path / "none.json")
    changelog._cache.update(v=None, mtime=None, path=None)
    assert changelog.feed(for_admin=True, requested_lang="tr")["releases"] == []


def test_media_serves_plain_names_from_the_folder_only(tmp_path, monkeypatch):
    (tmp_path / "shot-after.png").write_bytes(b"\x89PNG")
    (tmp_path / "notes.txt").write_text("x")
    monkeypatch.setattr(changelog, "MEDIA_DIR", tmp_path)
    found = changelog.media_file("shot-after.png")
    assert found is not None and found[1] == "image/png"
    for bad in ("../secret.png", "notes.txt", "missing.png", "Shot.PNG", "",
                ".hidden.png", "a/b.png"):
        assert changelog.media_file(bad) is None, bad


def test_shots_ignore_names_that_are_not_plain_files(feed_file, monkeypatch):
    data = json.loads(feed_file.read_text())
    data["entries"][1]["img"] = {"before": "../x.png", "after": "ok.png"}
    _write(feed_file.parent, monkeypatch, data)
    r = [x for x in changelog.releases() if x["id"] == "grid"][0]
    assert r["shots"] == {"after": "ok.png", "afterAlt": "New grid"}
