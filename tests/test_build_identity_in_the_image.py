"""The build stamp works in the container image too.

An installed package has no .git beside it, so every git call in build_info
came back empty inside the image and the stamp read as nothing: no version, no
commit. The image build now stamps both (Dockerfile ARGs, set by the release
workflow), and whoever runs the image can add its digest, which the image
cannot know about itself (external review 2026-09-24, DOC-05).
"""
from pathlib import Path

import pytest

from queryhub.web import build_info

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def no_git(monkeypatch):
    monkeypatch.setattr(build_info, "_git", lambda *a: "")
    monkeypatch.setattr(build_info, "_bcache", {"v": None, "t": 0.0})
    for k in ("QH_BUILD_SHA", "QH_BUILD_VERSION", "QH_IMAGE_DIGEST"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_the_image_reads_its_stamped_identity(no_git):
    no_git.setenv("QH_BUILD_SHA", "0123456789abcdef0123456789abcdef01234567")
    no_git.setenv("QH_BUILD_VERSION", "1.0.34")
    b = build_info.build()
    assert (b["sha"], b["version"]) == ("0123456", "v1.0.34")


def test_the_digest_comes_from_whoever_runs_the_image(no_git):
    no_git.setenv("QH_IMAGE_DIGEST", "sha256:" + "ab" * 32)
    assert build_info.build()["image"] == "sha256:" + "ab" * 32


def test_nothing_stamped_stays_empty(no_git):
    b = build_info.build()
    assert (b["sha"], b["version"], b["image"]) == ("", "", "")


def test_a_checkout_keeps_its_git_identity(monkeypatch):
    answers = {("rev-parse", "--short", "HEAD"): "8f4ace2",
               ("rev-list", "--count", "HEAD"): "708"}
    monkeypatch.setattr(build_info, "_git", lambda *a: answers.get(a, ""))
    monkeypatch.setattr(build_info, "_commit_date", lambda: "2026-09-25 09:57")
    monkeypatch.setattr(build_info, "_bcache", {"v": None, "t": 0.0})
    monkeypatch.setenv("QH_BUILD_SHA", "ffffffffffffffff")
    b = build_info.build()
    assert (b["sha"], b["version"]) == ("8f4ace2", "r708")


def test_the_image_build_is_given_the_values():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ARG QH_BUILD_SHA=""' in docker and 'ARG QH_BUILD_VERSION=""' in docker
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "QH_BUILD_SHA=${{ github.sha }}" in release
    assert "QH_BUILD_VERSION=${{ steps.meta.outputs.version }}" in release
