"""The version the docs advertise has to be the version that shipped.

Three files show an example image tag, and naming an explicit version there is
deliberate: a SQL gateway should not change underneath an operator because
`latest` moved. The cost of that advice is that the number has to be maintained,
and it was not — the README went on telling people to pull 1.0.0 after 1.0.1 was
published. A reader following it gets a real image, just not the one the project
is shipping, which is a worse failure than a broken link because nothing looks
wrong.

So the claim is checked rather than remembered.
"""
import pathlib
import re
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Every file that shows the reader a version to pull.
ADVERTISED_IN = ["README.md", ".env.example", "docker-compose.install.yml"]

_TAG = re.compile(r"database-queryhub:(\d+\.\d+\.\d+)")


def _version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


@pytest.mark.parametrize("rel", ADVERTISED_IN)
def test_the_advertised_image_tag_is_the_shipped_version(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    found = set(_TAG.findall(text))
    if not found:
        pytest.skip(f"{rel} advertises no image tag")
    assert found == {_version()}, (
        f"{rel} advertises {sorted(found)} but pyproject says {_version()}. "
        "Update the example tag, or the next reader pulls a version this "
        "project is no longer shipping.")


def test_the_changelog_documents_the_shipped_version():
    """A release whose notes are empty is a release nobody can read: the workflow
    extracts the section by heading, so a missing one ships a blank Release."""
    v = _version()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[?{re.escape(v)}\]?", changelog, re.M), (
        f"CHANGELOG.md has no section for {v}")


def test_the_changelog_keeps_older_sections():
    """History is not drift. The check above must not tempt anyone into
    rewriting old entries to match the current version."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = re.findall(r"^## \[?(\d+\.\d+\.\d+)\]?", changelog, re.M)
    assert len(versions) >= 2, "expected the current release and at least one before it"
    assert versions == sorted(versions, key=lambda s: [int(p) for p in s.split(".")],
                              reverse=True), (
        f"CHANGELOG sections are not newest-first: {versions}")
