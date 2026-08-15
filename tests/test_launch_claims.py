"""Four things a stranger meets in the first minute, each of which was untrue.

Not style. Each of these is a factual claim the project made that a new user
could act on and be wrong:

(a) The primary install command pulled `ghcr.io/<owner>/<repo>:0.1.0`. That image
    did not exist — the registry answered 403 for its manifest — so the README's
    two-command install failed for everyone who tried it. Releases publish an
    image now, but the install file still builds from the checkout by default and
    takes an image only when QH_IMAGE names one: a gateway should not change what
    it runs because a shared tag moved.
(b) The README advertised engines behind screenshots without saying which ones
    actually execute. Two do (postgres, mssql); clickhouse parses and is refused
    at execution; nothing else has a spec.
(c) The UI told users "Approvals still run in Slack" unconditionally, in a
    default profile that has no Slack.
(d) psycopg is LGPL-3.0 and nothing in the repo said so — the single fact most
    likely to stop an enterprise legal review.

These tests pin the claims rather than the prose, so rewording is free and
un-claiming is not.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (a) the install path must not reference an image that does not exist
# ---------------------------------------------------------------------------

def test_the_install_compose_does_not_hardcode_a_registry_image():
    """The failure this pins is a compose file that pulls a specific published
    tag by default. It shipped once with a tag that had never been published
    (the GHCR manifest answered 403), and it would be just as wrong with a tag
    that HAS been published: the version an install runs is the operator's
    choice, expressed through QH_IMAGE, not a default that moves under them."""
    compose = _read("docker-compose.install.yml")
    pull_refs = [ln for ln in compose.splitlines()
                 if re.match(r"\s*image:", ln) and "ghcr.io" in ln]
    assert not pull_refs, f"pulls a registry image by default: {pull_refs}"


def test_the_install_compose_builds_from_the_checkout():
    compose = _read("docker-compose.install.yml")
    assert re.search(r"^\s*build:", compose, re.M), "no build stanza"
    assert re.search(r"^\s*context: \.", compose, re.M)


def test_the_image_tag_is_still_overridable():
    """An operator with a prebuilt image must be able to use it, or this fix
    would trade one dead end for another."""
    assert "${QH_IMAGE:-" in _read("docker-compose.install.yml")


def test_the_readme_says_the_first_start_builds():
    """The surprise this removes is a multi-minute build with no explanation,
    which reads as a hang."""
    readme = _read("README.md")
    i = readme.index("## Install")
    section = readme[i:i + 1600]
    assert "builds the image from this checkout" in section
    # And says what to do instead of building, which is the half that was
    # missing while no image existed.
    assert "QH_IMAGE" in section


# ---------------------------------------------------------------------------
# (b) the engine claim must match engines.py
# ---------------------------------------------------------------------------

def test_the_readme_engine_table_matches_the_code():
    """Three states, and each has to be the state the code is actually in."""
    from dba_slack_bot import engines
    readme = _read("README.md")
    i = readme.index("## Engines")
    table = readme[i:i + 2600]

    assert sorted(engines.WIRED_ENGINES) == ["mssql", "postgres"], (
        "WIRED_ENGINES changed — the README table needs updating with it")
    for wired in ("PostgreSQL", "SQL Server"):
        assert wired in table
    assert "ClickHouse" in table
    # The middle state has to be stated as a refusal, not as a supported engine.
    assert "refuses to run" in table or "refuse" in table.lower()


def test_clickhouse_really_is_spec_only():
    """The README's middle row is a claim about behaviour; this is the
    behaviour. A spec exists, and execution is refused."""
    from dba_slack_bot import engines
    assert engines.spec("clickhouse").name == "clickhouse"
    assert engines.is_executable("clickhouse") is False
    assert engines.is_executable("postgres") is True
    assert engines.is_executable("mssql") is True


def test_no_engine_is_claimed_that_has_no_spec():
    """oracle / mysql / couchbase appeared in the prototype's logo map. The
    README must not present them as engines."""
    from dba_slack_bot import engines
    readme = _read("README.md")
    table = readme[readme.index("## Engines"):]
    table = table[:table.index("## Screenshots")]
    for absent in ("Oracle", "MySQL", "Couchbase"):
        assert absent not in table, f"README engine table mentions {absent}"
        # `spec()` falls back to the postgres spec for an unknown name rather
        # than raising, so `is_executable` is the gate that matters: a target
        # tagged with one of these is refused at execution rather than run
        # through the PostgreSQL driver.
        assert engines.is_executable(absent.lower()) is False


def test_the_screenshots_are_the_corrected_fleet():
    """They were re-shot from the corrected prototype mock, so the caveat that
    used to sit above them must be GONE — a stale disclaimer over honest images
    is its own kind of wrong. Files, not just prose: a caption pointing at a
    missing PNG renders as a broken image on the landing page."""
    readme = _read("README.md")
    shots = readme[readme.index("## Screenshots"):]
    shots = shots[:shots.index("## What it does")]
    assert "earlier prototype" not in shots, "the stale-image caveat is still there"
    for rel in ("flow.gif", "web/welcome.png", "web/editor.png", "web/grants.png",
                "web/audit.png", "web/editor-light.png", "web/login.png"):
        assert rel in shots, f"{rel} is not referenced"
        assert (ROOT / "docs" / "screenshots" / rel).exists(), f"{rel} is missing"


def test_the_hero_is_the_approval_screen():
    """The README's one-line claim is per-query approval, and for a long time no
    image showed it. The hero has to be that screen, above the fold, and the file
    has to exist."""
    readme = _read("README.md")
    head = readme[:readme.index("## How this compares")]
    assert "docs/screenshots/hero.png" in head
    assert (ROOT / "docs" / "screenshots" / "hero.png").exists()


def test_no_screenshot_ships_unreferenced():
    """An image in the repo that no page shows is either a leftover or an
    oversight, and both are worth failing on before a publish.

    Walks recursively: the directory has web/ and slack/ subfolders, and a
    top-level-only scan would have silently stopped checking the moment they
    were introduced."""
    readme = _read("README.md")
    root = ROOT / "docs" / "screenshots"
    shipped = [p for p in root.rglob("*") if p.is_file() and p.suffix != ".md"]
    assert shipped, "no screenshots found — the glob is wrong"
    unused = sorted(str(p.relative_to(ROOT)) for p in shipped
                    if str(p.relative_to(ROOT)) not in readme)
    assert not unused, f"in docs/screenshots but not in the README: {unused}"


def test_every_referenced_screenshot_exists():
    """The other direction: a caption pointing at a missing file renders as a
    broken image on the landing page, which is worse than no image."""
    import re
    readme = _read("README.md")
    refs = sorted(set(re.findall(r"docs/screenshots/[\w/.-]+\.(?:png|gif)", readme)))
    assert len(refs) >= 10, f"only {len(refs)} screenshots referenced"
    missing = [r for r in refs if not (ROOT / r).exists()]
    assert not missing, f"referenced but absent: {missing}"


def test_the_screenshot_folder_documents_itself():
    """The index is how the next person knows these are mock-rendered rather than
    captures of a live deployment — which is the whole reason they are safe to
    publish."""
    idx = ROOT / "docs" / "screenshots" / "README.md"
    assert idx.exists(), "docs/screenshots/README.md is missing"
    body = idx.read_text(encoding="utf-8")
    for f in ("hero.png", "flow.gif", "web/audit.png", "slack/slack-modal.png"):
        assert f in body, f"{f} is not described in the index"


# ---------------------------------------------------------------------------
# (c) the Slack claim must be conditional — backend half
# ---------------------------------------------------------------------------

def test_me_exposes_whether_slack_is_enabled():
    """The frontend had no way to know: `slackEnabled` did not exist anywhere in
    the payload, so the copy could not be conditional even in principle."""
    src = _read("src/dba_slack_bot/web/app.py")
    assert 'out["slackEnabled"] = bool(cfg.ENV.slack_enabled)' in src


def test_the_flag_is_the_same_one_the_backend_gates_on():
    """Two sources of truth for "is there a Slack" would let the copy drift from
    the behaviour, which is the bug rather than the fix."""
    src = _read("src/dba_slack_bot/web/app.py")
    assert "cfg.ENV.slack_enabled" in src
    gates = _read("src/dba_slack_bot/web/routes_queries.py")
    assert "cfg.ENV.slack_enabled" in gates


def test_the_unconditional_copy_is_recorded_for_the_prototype():
    """The affected strings live in the prototype components, which are
    refreshed wholesale rather than patched here, so the required change is
    written down for whoever does that refresh. This asserts the note exists —
    otherwise the backend half ships and the visible half never changes.

    Skipped when the design working copy is not in the checkout: it is a local
    reference tree, not something every checkout has."""
    path = ROOT / "QueryHubWeb" / "_upstream_only" / "BRIEF.md"
    if not path.exists():
        pytest.skip("design working copy not in this checkout")
    brief = path.read_text(encoding="utf-8")
    assert "Approvals still run in Slack" in brief
    assert "slackEnabled" in brief
    assert "qh-home.jsx" in brief and "qh-app.jsx" in brief


# ---------------------------------------------------------------------------
# (d) the licences that are actually shipped
# ---------------------------------------------------------------------------

def test_third_party_notices_exists_and_names_the_copyleft_dependency():
    notices = _read("THIRD_PARTY_NOTICES.md")
    assert "psycopg" in notices
    assert "LGPL" in notices


def test_every_declared_dependency_appears_in_the_notices():
    """A notices file that drifts is worse than none: it reads as complete.
    Checked against pyproject rather than a hand-kept list."""
    import tomllib
    proj = tomllib.loads(_read("pyproject.toml"))["project"]
    notices = _read("THIRD_PARTY_NOTICES.md")
    specs = list(proj.get("dependencies") or [])
    for extra in (proj.get("optional-dependencies") or {}).values():
        specs.extend(extra)
    missing = []
    for spec in specs:
        name = re.split(r"[<>=!\[; ]", spec.strip())[0]
        if name.lower() not in notices.lower():
            missing.append(name)
    assert not missing, f"not mentioned in THIRD_PARTY_NOTICES.md: {missing}"


def test_the_notices_match_the_installed_licence_metadata():
    """The point of the file is that it is true. Read the licences from the
    installed distributions and confirm the copyleft ones are all called out."""
    import importlib.metadata as md
    notices = _read("THIRD_PARTY_NOTICES.md")
    copyleft = []
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if not name or name.lower() in ("dba-slack-bot", "queryhub"):
            continue
        lic = (dist.metadata.get("License-Expression") or "")
        cls = " ".join(c for c in dist.metadata.get_all("Classifier") or []
                       if c.startswith("License ::"))
        blob = f"{lic} {cls}".lower()
        if "gpl" in blob or "mozilla" in blob or "mpl" in blob:
            copyleft.append(name)
    unlisted = [n for n in copyleft if n.lower() not in notices.lower()]
    assert not unlisted, (
        f"copyleft dependencies missing from THIRD_PARTY_NOTICES.md: {unlisted}")


# Which vendor owns the mark in each engine SVG. Derived from the FILE NAMES so
# the check cannot drift: adding a logo without an owner here fails, and so does
# shipping one whose owner is not named in the notices.
_ENGINE_TRADEMARK_OWNER = {
    "postgres": "PostgreSQL",
    "mssql": "Microsoft",
    "clickhouse": "ClickHouse",
    "oracle": "Oracle",
    "mysql": "MySQL",
    "couchbase": "Couchbase",
}


def test_the_engine_logos_are_attributed():
    """Every vendor trademark that SHIPS is attributed — read off the directory
    rather than a hand-kept list.

    It used to name all six, which stopped being true on 2026-08-15 when
    `oracle`, `mysql` and `couchbase` were deleted: no engine spec exists for
    them, so no connection could ever have rendered one, and they were
    advertising capability the product does not have. A list in a test is the
    wrong place to learn that — it would have gone on demanding attribution for
    files that are not here, or (worse, later) stayed silent when a new logo
    arrived unattributed."""
    from pathlib import Path
    engines = Path(__file__).resolve().parent.parent / "QueryHubWeb" / "brand" / "engines"
    shipped = sorted(p.stem for p in engines.glob("*.svg"))
    assert shipped, "no engine logos found — did the directory move?"

    notices = _read("THIRD_PARTY_NOTICES.md")
    assert "trademark" in notices.lower()
    for stem in shipped:
        owner = _ENGINE_TRADEMARK_OWNER.get(stem)
        assert owner, (f"{stem}.svg ships with no known trademark owner — add "
                       f"it to _ENGINE_TRADEMARK_OWNER and to the notices")
        assert owner in notices, f"{stem}.svg ships but {owner} is not attributed"


def test_the_msodbc_eula_is_disclosed():
    """The SQL Server driver is not open source and is not installed by
    QueryHub — an extra that quietly needs a proprietary EULA is the kind of
    surprise this file exists to remove."""
    notices = _read("THIRD_PARTY_NOTICES.md")
    assert "msodbcsql18" in notices
    assert "EULA" in notices


def test_the_readme_points_at_the_notices():
    combined = _read("README.md") + _read("NOTICE")
    assert "THIRD_PARTY_NOTICES" in combined


def test_me_actually_returns_the_flag_over_http(monkeypatch):
    """The source assertion above says the line exists; this says the payload
    carries it. A frontend cannot read a field the endpoint does not send, and
    that was the original state — `slackEnabled` appeared nowhere at all."""
    import logging
    from starlette.testclient import TestClient
    from dba_slack_bot import admins, db, requesters
    from dba_slack_bot.web import app as web_app, sessions
    from dba_slack_bot.slack_app import notifications

    logging.disable(logging.CRITICAL)
    monkeypatch.setattr(db, "init_pool", lambda: None)
    monkeypatch.setattr(notifications, "dm_all_admins", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "verify_access",
                        lambda t: {"sub": "U0X", "sid": "s", "provider": "slack"}
                        if t == "good" else None)
    monkeypatch.setattr(sessions, "session_alive", lambda s, principal=None: True)
    monkeypatch.setattr(admins, "is_admin", lambda u: False)
    monkeypatch.setattr(requesters, "is_allowed", lambda u: True)
    try:
        with TestClient(web_app.create_app()) as c:
            c.cookies.set("qh_session", "good")
            body = c.get("/api/me").json()
        assert "slackEnabled" in body, sorted(body)
        assert isinstance(body["slackEnabled"], bool)
    finally:
        logging.disable(logging.NOTSET)
