"""The denylist reads the model that holds the data, not the one that used to.

`check_repo_clean.py` keeps real names out of the repository by pulling them
live from the bot DB. That makes it exactly as good as the tables it reads, and
on 2026-09-08 those tables stopped being the ones with the data in them: the pod
cutover emptied the legacy `teams` and `team_members`, so a scan whose whole
purpose is catching real team names went from 285 patterns to seeing no team
name at all. Silently -- the run said `clean`.

It was caught by a commit message naming two pods and a production alias. The
alias was flagged (`target_servers` is still the live table); the two pod names
were waved through. Fixing the source turned up four more hits in tracked files
that had been committed under the blind scan.

The lesson is not "read `team` instead of `teams`" -- that just moves the blind
spot to the next migration. It is that a denylist over a model in transition
reads BOTH sides, because the cost of a stale union is a redundant pattern and
the cost of a missing one is a leak nobody sees.
"""
import pathlib

import pytest

_PATH = (pathlib.Path(__file__).resolve().parents[1]
         / "scripts" / "check_repo_clean.py")
_HAVE = _PATH.exists()
upstream_only = pytest.mark.skipif(
    not _HAVE, reason="check_repo_clean.py is upstream-only")
SRC = _PATH.read_text(encoding="utf-8") if _HAVE else ""


def _q(label: str) -> str:
    """The source of the query that produces `label`, found by its label and
    read backward to the `db.fetch_all(` that fetched it.

    Anchoring on the SQL itself does not work -- two of these queries open with
    the same `SELECT DISTINCT name FROM (` -- and anchoring forward from a
    comment does not either, because the comment explaining a query is longer
    than any fixed window. So pass the label in its f-string form, which is the
    one place each string appears exactly once: the prose above a query quotes
    the label too, and matching that found the query before it."""
    end = SRC.index(label)
    start = SRC.rindex("rows = db.fetch_all(", 0, end)
    return SRC[start:end]


@upstream_only
def test_team_names_come_from_both_models():
    """`teams` is empty and stays empty; `team` is where a team lives now. A
    scan that reads one of them is a scan that works on one side of a switch."""
    block = _q('f"real team name')
    assert "FROM teams" in block, "the legacy table is still a source"
    assert "FROM team " in block, "so is the new one"


@upstream_only
def test_a_team_is_denied_by_its_label_as_well_as_its_code():
    """People write "Team A" in prose and `team-a` in code, and both name the
    same real pod. `team` stores them in different columns, so a scan that took
    only `name` would catch the code and publish the label.

    Written with placeholders on the second attempt: the first used a real pod's
    code and label, and the scan this file is about caught its own test."""
    assert "display_name FROM team" in _q('f"real team name')


@upstream_only
def test_slack_ids_come_from_both_models_too():
    """Same fault, same shape: `team_members` was emptied by the same cutover,
    and an identity reachable only through `principal_identity` would be
    invisible."""
    block = _q('f"real Slack user ID')
    assert "FROM team_members" in block
    assert "FROM principal_identity" in block
    assert "provider = 'slack'" in block


@upstream_only
def test_a_dropped_team_is_still_a_real_name():
    """The query does not filter `is_deleted`. A team that was disbanded still
    names something that existed, and the four legacy teams the cutover dropped
    are exactly the names most likely to be sitting in a draft comment."""
    assert "is_deleted" not in _q('f"real team name')


@upstream_only
def test_a_bare_common_word_is_still_exempt():
    """Two of the real pods are called "Data" and "Platform". Denying those
    would fail every ordinary sentence, which is the failure mode that gets a
    gate switched off -- so the exemption has to survive this change."""
    assert "generic_words" in SRC
    for word in ("platform", "data"):
        assert f'"{word}"' in SRC, word


# ---------------------------------------------------------------------------
#
# The same argument, one rung down: which CATALOGUED DATABASE NAME is worth
# denying. That was a hand-curated set of ordinary words until enabling three
# endpoints catalogued databases called `status`, `people`, `example` and
# `review`, and the next scan reported 2457 hits in 319 files. A gate that
# cannot go green is a gate its reader learns to skip, so the shape decides now.

def _scanner():
    """The scanner as a module. Importing it does not touch the database --
    every query in it is inside a function."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_check_repo_clean", _PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:  # pragma: no cover - dependency missing in this env
        pytest.skip("check_repo_clean.py needs the bot package to import")
    return mod


@upstream_only
def test_a_bare_english_word_is_not_a_database_leak():
    """`<server>/<database>` names a service twice, but only when the database
    half names anything. On its own `status` is a word, and denying it fails
    every sentence that contains it."""
    is_distinctive = _scanner()._database_is_distinctive
    for word in ("status", "people", "example", "review", "compliance",
                 "terminal", "finance", "marketing", "inventory"):
        assert not is_distinctive(word), word


@upstream_only
def test_a_service_database_is_still_denied():
    """Every catalogued name that actually names a service is a compound --
    measured over all 91 of them on 2026-09-17. The names here are invented for
    the same reason this scan exists: a test that spelled the real ones would
    be the leak it is testing for."""
    is_distinctive = _scanner()._database_is_distinctive
    for name in ("example_service", "alpha_integration_service", "betadb_log",
                 "SomeAdminDb", "XPLACEHOLDER", "example_service_rollback"):
        assert is_distinctive(name), name


@upstream_only
def test_the_two_hand_kept_lists_still_decide_their_own_cases():
    """A bare word that is not English still names something, and a compound
    can still be operator-internal -- `qh_pilot` is named in an applied
    migration whose checksum makes it immutable. Both lists are read from the
    scanner rather than spelled out here, for the reason above."""
    mod = _scanner()
    is_distinctive = mod._database_is_distinctive
    assert mod._DISTINCTIVE_DATABASES and mod._INTERNAL_DATABASES
    for name in mod._DISTINCTIVE_DATABASES:
        assert is_distinctive(name), name
    for name in mod._INTERNAL_DATABASES:
        assert not is_distinctive(name), name
    assert not is_distinctive("nova")  # under six characters
