"""The public tree shows the Warm theme in What's-new screenshots.

The source tree keeps the default-theme copy at docs/whatsnew/<name>.png. A
Warm copy at docs/whatsnew/oss/<name>.png replaces it in the export.
"""
import importlib.util
import pathlib

import pytest

_PATH = (pathlib.Path(__file__).resolve().parents[1]
         / "scripts" / "export_independent_repo.py")
upstream_only = pytest.mark.skipif(
    not _PATH.exists(), reason="the exporter is upstream-only")


def _overlay():
    spec = importlib.util.spec_from_file_location("_export_repo", _PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:  # pragma: no cover - dependency missing in this env
        pytest.skip("the exporter needs its own imports")
    return mod.whatsnew_overlay


@upstream_only
def test_a_warm_copy_replaces_the_private_copy():
    files = [
        "docs/whatsnew/a-before.png",
        "docs/whatsnew/oss/a-before.png",
        "docs/whatsnew/b-after.png",
    ]
    skip, dest = _overlay()(files)
    assert skip == {"docs/whatsnew/a-before.png"}
    assert dest == {"docs/whatsnew/oss/a-before.png": "docs/whatsnew/a-before.png"}


@upstream_only
def test_a_file_without_a_warm_copy_is_exported_as_it_is():
    skip, dest = _overlay()(["docs/whatsnew/b-after.png", "README.md"])
    assert skip == set()
    assert dest == {}
