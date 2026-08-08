"""The commit-message scan skips git's attribution trailers, and nothing else.

A squash merge on GitHub appends `Co-authored-by: <name> <id+user@users.noreply.
github.com>` for every contributor, so the repository owner's own name is written
into the message by the platform. That name is legitimately on the dynamic
denylist, so the scan failed on a week-old commit and the only remedy it could
offer was rewriting history.

The exemption has to be narrow or it defeats the scan it lives in: the reason the
message scan exists at all is that a production alias once sat in a commit BODY.
So these tests come in pairs — the trailer is skipped, the same string in prose
is not.
"""
import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_repo_clean",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_repo_clean.py")
crc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(crc)

strip = crc._strip_attribution

NAME = "Jane Q Public"


def test_a_generated_trailer_is_dropped():
    body = f"add a thing\n\nWhy it matters.\n\nCo-authored-by: {NAME} <1+jq@users.noreply.github.com>\n"
    assert NAME not in strip(body)


def test_signed_off_by_too():
    assert NAME not in strip(f"subject\n\nSigned-off-by: {NAME} <jq@example.com>\n")


def test_capitalisation_does_not_matter():
    # Our own convention writes `Co-Authored-By`; GitHub writes `Co-authored-by`.
    for form in ("Co-Authored-By", "co-authored-by", "CO-AUTHORED-BY"):
        assert NAME not in strip(f"subject\n\n{form}: {NAME} <jq@example.com>\n")


# ---------------------------------------------------------------------------
# the other half of each pair: the body is still read in full
# ---------------------------------------------------------------------------

def test_the_same_name_in_prose_survives():
    """If this ever passes by being stripped, the exemption has eaten the scan."""
    body = f"add a thing\n\n{NAME} asked for this.\n\nCo-authored-by: {NAME} <jq@example.com>\n"
    out = strip(body)
    assert NAME in out, "a name written in the message body must still be scanned"
    assert out.count(NAME) == 1, "only the trailer occurrence should be removed"


def test_an_alias_in_the_body_survives():
    body = "port design round\n\nRewritten because svc-prod-orders is the sample fleet.\n"
    assert "svc-prod-orders" in strip(body)


def test_a_trailer_like_sentence_is_not_a_trailer():
    """Only a real trailer — start of line, then a colon — is removed. A sentence
    that mentions one is prose and stays."""
    body = "subject\n\nWe should add a Co-authored-by trailer for svc-prod-orders work.\n"
    assert "svc-prod-orders" in strip(body)


def test_indented_trailers_still_count():
    # git allows leading whitespace on trailers; a scanner that missed those
    # would fail on some merges and not others.
    assert NAME not in strip(f"subject\n\n   Co-authored-by: {NAME} <jq@example.com>\n")


def test_a_body_with_no_trailer_is_returned_unchanged():
    body = "subject\n\nbody line\n"
    assert strip(body) == body


@pytest.mark.parametrize("field", ["author", "committer"])
def test_the_gap_this_does_not_close_is_documented(field):
    """The commit's own author/committer fields are outside any content scan.
    That is why the git identity must be set before a repo is initialised, and
    the docstring has to keep saying so."""
    assert field in strip.__doc__
