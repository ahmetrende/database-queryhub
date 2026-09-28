"""The S3 metrics dashboard wears the admin panel's look.

The page is one self-contained HTML file on S3, so it cannot link the web app's
stylesheet or fonts: it copies the panel's classes and inlines the panel's own
typeface at build time. These pin the pieces that can break without anyone
opening the page — the font inlining, and the section shells the renderer
fills.
"""
import base64
import importlib.util
import re
from pathlib import Path

BUILDER = Path(__file__).resolve().parents[1] / "scripts" / "build_metrics_dashboard.py"

PLACEHOLDERS = ("%FONT_FACES%", "%GENERATED_AT%", "%VIEW_SUB%", "%TOC%",
                "%SECTIONS%", "%DATA%", "%RENDERER_JS%")


def _builder():
    spec = importlib.util.spec_from_file_location("build_metrics_dashboard", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stylesheet(tmp_path, faces):
    (tmp_path / "fonts").mkdir()
    blocks = "".join(
        "@font-face {\n"
        f"  font-family: '{family}';\n"
        f"  src: url('./fonts/{name}') format('woff2');\n"
        f"  font-weight: {weight};\n"
        "}\n"
        for family, weight, name in faces)
    css = tmp_path / "type.css"
    css.write_text("/* tokens */\n" + blocks + ":root { --x: 1; }\n", encoding="utf-8")
    return css


def _payload():
    return {"rows": [], "annotations": [], "teams": [], "users": [], "targets": [],
            "databases": [], "who_can_what": [], "rating_low": [], "csv_imports": [],
            "config": {"report_start_date": "2026-05-01",
                       "report_timezone": "Europe/Istanbul"},
            "generated_at": "28 Sep 2026 · 14:05 TR"}


def test_only_the_named_family_is_inlined(tmp_path):
    b = _builder()
    css = _stylesheet(tmp_path, [("Alpha", 400, "a400.woff2"),
                                 ("Alpha", 700, "a700.woff2"),
                                 ("Beta Mono", 400, "b400.woff2")])
    for name in ("a400.woff2", "a700.woff2", "b400.woff2"):
        (tmp_path / "fonts" / name).write_bytes(name.encode())

    out = b.embedded_font_faces(css, "Alpha")

    assert out.count("@font-face") == 2
    assert "Beta Mono" not in out
    assert "data:font/woff2;base64," + base64.b64encode(b"a700.woff2").decode() in out
    assert "./fonts/" not in out, "a relative url would 404 next to the page on S3"


def test_a_face_whose_file_is_missing_is_left_out(tmp_path):
    b = _builder()
    css = _stylesheet(tmp_path, [("Alpha", 400, "a400.woff2"),
                                 ("Alpha", 700, "gone.woff2")])
    (tmp_path / "fonts" / "a400.woff2").write_bytes(b"x")

    out = b.embedded_font_faces(css, "Alpha")

    assert out.count("@font-face") == 1
    assert "gone.woff2" not in out
    assert b.embedded_font_faces(tmp_path / "absent.css", "Alpha") == ""


def test_the_configured_typeface_exists():
    """A wrong path or family name does not fail the build — the page quietly
    falls back to system fonts — so it has to fail here."""
    out = _builder().embedded_font_faces()
    assert out.count("@font-face") >= 1
    assert ";base64," in out


def test_the_page_is_laid_out_like_the_admin_panel():
    b = _builder()
    page = b.render(_payload())

    for ph in PLACEHOLDERS:
        assert ph not in page, ph
    assert "From p_metrics_* (self-test excluded) · since 2026-05-01 · Europe/Istanbul" in page

    groups = [g for g in dict.fromkeys(spec[3] for spec in b.CHART_SPECS) if g]
    assert page.count("class='qh-msection'") == len(groups)
    assert page.count("<div class='qh-mgrid'>") == len(groups)
    assert page.count("class='qh-chip'") == len(groups)
    assert "Outcomes &amp; approvals" in page

    for spec_id, _title, factory, _group, layout in b.CHART_SPECS:
        assert f"id='canvas-{spec_id}'" in page
        assert re.search(r"FACTORIES\." + factory + r"\s*=", b.RENDERER_JS), factory
        assert layout in ("bare", "half", "wide")
    # The headline numbers sit on the page itself, as in the panel, not in a card.
    assert "<section id='sec-kpi-headline'" in page
