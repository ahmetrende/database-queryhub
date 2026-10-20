"""A name dropped on the editor goes where the caret is.

The editor used to turn the pointer position into a character index and insert
there. A person drags a database in from the sidebar and lets go near the left
edge of the editor, so the name landed at the start of the line, away from the
statement being typed. The drop now works like a paste: at the caret, replacing a
selection. These tests read the editor file as a contract; the behaviour was also
checked in a browser (caret insert, three drop points, a selection, undo).
"""
from pathlib import Path

EDITOR = (Path(__file__).resolve().parents[1] / "QueryHubWeb" / "qh-editor.jsx").read_text(encoding="utf-8")


def _on_drop() -> str:
    return EDITOR.split("const onDrop = (e) => {", 1)[1].split("const onDragOver", 1)[0]


def test_the_drop_reads_the_caret_not_the_pointer():
    body = _on_drop()
    assert "ta.selectionStart" in body and "ta.selectionEnd" in body
    assert "clientX" not in body and "clientY" not in body
    assert "indexFromPoint" not in EDITOR, "the pointer-to-index mapping is gone on purpose"


def test_the_drop_replaces_a_selection_like_a_paste_and_keeps_undo():
    body = _on_drop()
    assert "value.slice(0, a) + text + value.slice(b)" in body
    assert "commit(" in body and "'drop'" in body        # one undo step, kind `drop`


def test_the_caret_ends_after_the_inserted_text():
    body = _on_drop()
    assert "ta.selectionStart = ta.selectionEnd = at" in body
