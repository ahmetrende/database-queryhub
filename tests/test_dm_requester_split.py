"""dm_requester must split long plain text into valid section blocks."""
from types import SimpleNamespace

from queryhub.slack_app import notifications as n


def _texts(sections):
    return [s["text"]["text"] for s in sections]


def test_short_text_is_one_section():
    sections = n.split_text_to_sections("hello\nworld")
    assert _texts(sections) == ["hello\nworld"]
    assert sections[0]["type"] == "section"


def test_long_multiline_text_is_split_on_lines():
    lines = [f"line {i} " + "x" * 90 for i in range(100)]
    sections = n.split_text_to_sections("\n".join(lines))
    texts = _texts(sections)
    assert len(texts) > 1
    assert all(len(t) <= 3000 for t in texts)
    rebuilt = [ln for t in texts for ln in t.split("\n")]
    assert rebuilt == lines


def test_single_long_line_is_cut_at_limit():
    sections = n.split_text_to_sections("a" * 7000)
    texts = _texts(sections)
    assert [len(t) for t in texts] == [3000, 3000, 1000]
    assert "".join(texts) == "a" * 7000


def test_dm_requester_posts_valid_blocks(monkeypatch):
    monkeypatch.setattr(n, "cfg", SimpleNamespace(
        ENV=SimpleNamespace(slack_enabled=True)))
    monkeypatch.setattr(n, "_display_overrides", lambda: {})
    posts = []
    monkeypatch.setattr(
        n, "_post", lambda client, **kw: posts.append(kw) or {"channel": "C1"})
    client = SimpleNamespace(
        conversations_open=lambda users: {"channel": {"id": "C1"}})
    # About 168,000 characters: more than 45 sections, so two messages.
    lines = [f"change {i} " + "y" * 200 for i in range(800)]
    channel = n.dm_requester(client, "U1", "\n".join(lines))
    assert channel == "C1"
    assert len(posts) >= 2
    seen = []
    for kw in posts:
        assert len(kw["blocks"]) <= 45
        for b in kw["blocks"]:
            assert len(b["text"]["text"]) <= 3000
            seen.extend(b["text"]["text"].split("\n"))
        assert len(kw["text"]) <= 35000
    assert seen == lines
