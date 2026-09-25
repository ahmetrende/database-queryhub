"""The container image installs a hash-locked dependency set.

External review 2026-09-24 (SUP-01): the Dockerfile ran `pip install .`, which
resolved the ranges in pyproject.toml at build time. Two builds of one commit
could ship different wheels, and nothing checked what was downloaded. The image
now installs docker/requirements.lock with --require-hashes, then the package
with --no-deps.

A lock that drifts from pyproject.toml is a silent version of the old problem:
the image would install whatever the lock says while pyproject promises
something else. So these fail until the lock is regenerated
(scripts/lock_image_deps.sh) in the same change as the dependency.
"""
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "docker" / "requirements.lock"

_PIN = re.compile(r"^([A-Za-z0-9._-]+)==([^\s;\\]+)")


def _pins() -> dict[str, str]:
    pins = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        m = _PIN.match(line)
        if m:
            pins[canonicalize_name(m.group(1))] = m.group(2)
    return pins


def _declared() -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    reqs = [Requirement(r) for r in project["project"]["dependencies"]]
    reqs += [Requirement(r) for r in project["build-system"]["requires"]]
    return reqs


def test_every_declared_dependency_is_pinned_within_its_range():
    pins = _pins()
    for req in _declared():
        name = canonicalize_name(req.name)
        assert name in pins, f"{req.name} is not in {LOCK.name}: run scripts/lock_image_deps.sh"
        assert req.specifier.contains(pins[name], prereleases=True), (
            f"{req.name}=={pins[name]} in the lock is outside pyproject's {req.specifier}")


def test_every_pin_carries_a_hash():
    """--require-hashes refuses the whole install over one missing hash, which
    is the point; this says which one before the image build does."""
    text = LOCK.read_text(encoding="utf-8")
    blocks = re.split(r"\n(?=[A-Za-z0-9._-]+==)", text)
    pinned = [b for b in blocks if _PIN.match(b)]
    assert len(pinned) == len(_pins()) > 20
    for block in pinned:
        assert "--hash=sha256:" in block, block.splitlines()[0]


def test_the_image_installs_the_lock_and_nothing_else():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock" in docker
    assert "pip install --no-cache-dir --no-deps --no-build-isolation ." in docker
    assert "pip install --no-cache-dir . " not in docker


def test_the_release_audits_the_lock_and_blocks():
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    step = release[release.index("Dependency audit of the image's locked set"):]
    step = step[:step.index("- name:", 10)]
    assert "pip-audit -r docker/requirements.lock" in step
    assert "continue-on-error" not in step


def test_the_lock_follows_the_image_python():
    """The lock script reads the version from the Dockerfile, so the two cannot
    disagree after a base-image bump, only fall behind until it is rerun."""
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    py = re.search(r"^FROM python:([0-9.]+)-slim.* AS runtime$", docker, re.M).group(1)
    header = LOCK.read_text(encoding="utf-8").splitlines()[1]
    assert f"--python-version {py}" in header
