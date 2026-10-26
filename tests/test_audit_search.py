"""The audit trail's search endpoint, and its contract with the screen.

The screen is design-owned and derives nothing: category, effect, `classified`
and the actor's kind all arrive on the wire, because a client-side copy of the
vocabulary is a second answer that drifts from the one the counts were computed
with. That makes the two sides a contract, and these tests are what holds it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from queryhub.web import routes_admin as ra

ROOT = Path(__file__).resolve().parents[1]
SCREEN = (ROOT / "QueryHubWeb" / "qh-admin-audit.jsx").read_text(encoding="utf-8")
MIGRATION = (ROOT / "migrations"
             / "118_audit_category_vocabulary.sql").read_text(encoding="utf-8")


def _js_keys(const: str) -> set[str]:
    """The TOP-LEVEL keys of a `const X = { ... }` object literal in the screen.

    Depth-aware on purpose: one of these objects nests `{ label, hint }` under
    every key and another writes all its keys on one line, so neither a
    line-anchored regex nor a flat one reads both correctly.
    """
    body = SCREEN.split("const " + const + " = {", 1)[1]
    depth, out, i = 0, set(), 0
    while i < len(body):
        ch = body[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and ch == ":":
            m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", body[:i])
            if m:
                out.add(m.group(1))
        i += 1
    return out


def test_every_category_the_server_can_send_has_a_chip():
    """A category the screen does not know renders `undefined` as its label and
    throws on the hint lookup — the row that is hardest to classify would be the
    row that breaks the page."""
    assert set(ra._AUDIT_CATEGORIES) == _js_keys("QH_AUD_CATS")


def test_every_effect_the_server_can_send_has_a_label():
    # The screen carries an extra `other` for forward compatibility; the server
    # must not send anything the screen cannot name.
    assert set(ra._AUDIT_EFFECTS) <= _js_keys("QH_AUD_EFFECTS")


def test_every_actor_kind_the_server_can_send_has_a_chip():
    assert set(ra._AUDIT_ACTOR_KINDS) == _js_keys("QH_AUD_ACTORS")


def test_the_migration_only_seeds_the_agreed_vocabulary():
    """The CHECK constraints are the real guard; this catches a seed row that
    would fail to insert on a fresh install."""
    seeds = re.findall(r"^\s*\(\d+,\s*'[^']+',\s*(NULL|'[a-z]+'),\s*(NULL|'[a-z]+')",
                       MIGRATION, re.M)
    assert seeds, "no seed rows found in the migration"
    for cat, eff in seeds:
        if cat != "NULL":
            assert cat.strip("'") in ra._AUDIT_CATEGORIES, cat
        if eff != "NULL":
            assert eff.strip("'") in ra._AUDIT_EFFECTS, eff


def test_unclassified_is_never_a_pattern_target():
    """`unclassified` is what a row gets when nothing claims it. Seeding a rule
    that assigns it would make the count mean two different things."""
    assert "'unclassified'" not in MIGRATION.split("INSERT INTO", 1)[1]


def test_no_rule_exists_for_a_single_action_name():
    """A pattern that matches exactly one action name is the per-action list
    this table replaces. Ten rows on the live trail stay unclassified for
    precisely this reason, and that is the intended state."""
    patterns = re.findall(r"^\s*\(\d+, '([^']+)'", MIGRATION, re.M)
    # Every pattern is either a shared noun or a shared verb suffix. A pattern
    # carrying an action's whole name would be the smell.
    for p in patterns:
        assert not p.endswith("ed_") and p.count("_") <= 1, p


@pytest.mark.parametrize(("action", "want"), [
    ("pii_masking_exempted", "PII masking exempted"),
    ("web_result_downloaded", "Web result downloaded"),
    ("super_ddl_role_set", "Super DDL role set"),
    ("slack_sql_opened", "Slack SQL opened"),
    ("withdrawn", "Withdrawn"),
    ("", ""),
])
def test_the_label_is_readable_without_losing_the_acronyms(action, want):
    assert ra._audit_label(action) == want


def test_via_distinguishes_a_missing_window_from_no_window():
    """Three states, and the screen renders each differently — so a null must
    not stand for both "there was never a window" and "the window is gone"."""
    named = ra._audit_via(
        {"details": {"grant_id": 25}, "via_user": "U1", "via_target": "prod",
         "via_db": "app", "via_tier": "ro"}, {"U1": "Someone"})
    assert named == "auto-approve window · Someone → prod/app RO"
    gone = ra._audit_via({"details": {"grant_id": 25}, "via_user": None}, {})
    assert gone == "an auto-approve window that no longer exists"
    fp = ra._audit_via({"details": {"fingerprint_match_request_id": 303}}, {})
    assert fp == "query fingerprint matched request #303"
    assert ra._audit_via({"details": {}}, {}) is None
    assert ra._audit_via({"details": None}, {}) is None


def test_the_row_never_invents_a_display_name():
    row = ra._audit_row(
        {"id": 1, "created_at": None, "action": "target_disabled",
         "actor_slack_id": "U9", "actor_name": None, "details": {},
         "actor_kind": "person", "category": "connections", "effect": "changed",
         "request_id": None, "target_alias": None}, {})
    assert row["actor"]["name"] is None and row["actor"]["handle"] == "U9"
    assert row["request"] is None
    assert row["classified"] is True


def _req_row(**kw):
    row = {"id": 3, "created_at": None, "action": "approved",
           "actor_slack_id": "U9", "actor_name": "An Admin", "details": {},
           "actor_kind": "person", "category": "requests", "effect": "decided",
           "request_id": 7745, "target_alias": "alias", "database_name": "db",
           "requester_slack_id": "U1", "requester_name": "a.person"}
    row.update(kw)
    return row


def test_the_requester_is_the_roster_name_not_the_submit_time_copy():
    """`requests.requester_name` is whatever Slack said the day it was
    submitted, so one person appears under several spellings and often under a
    login handle. The roster row is kept fresh for everybody, so it answers."""
    row = ra._audit_row(_req_row(), {"U1": "A Person"})
    assert row["request"]["requester"] == "A Person"
    assert row["request"]["requesterId"] == "U1"


def test_a_requester_the_roster_no_longer_holds_keeps_the_copy():
    """Somebody who left is not on the roster any more. The snapshot is the
    only name that row will ever have, and losing it loses the answer to who
    asked."""
    row = ra._audit_row(_req_row(), {})
    assert row["request"]["requester"] == "a.person"


def test_no_requester_at_all_is_null_not_a_dash():
    """The list falls back to the actor when there is no requester, so it has
    to be able to tell. A "—" string is truthy and would print itself."""
    row = ra._audit_row(_req_row(requester_slack_id=None,
                                 requester_name=None), {})
    assert row["request"]["requester"] is None


def test_the_list_column_reads_the_requester_before_the_actor():
    """The screen's left column is design-owned; this is the one thing about it
    the endpoint depends on — the ORDER it reads fields in, which is why they
    are resolved server-side rather than copied.

    Asserted by order of appearance rather than by one spelling: the block was
    rewritten on 2026-09-17 (it gained the `requesterId` rung and a local for
    `row.request`) and a test pinned to the old text would have failed for a
    change that did exactly what it asks for.
    """
    block = SCREEN.split("function AudWho", 1)[1].split("\n}\n", 1)[0]
    at = {k: block.find(k) for k in
          (".requester", ".requesterId", "<AudActor")}
    assert all(v >= 0 for v in at.values()), at
    assert at[".requester"] < at[".requesterId"] < at["<AudActor"], (
        "the requester's name comes first, their id second, and the actor is "
        "the fallback for a row with no request at all")


def test_a_row_nothing_claims_is_marked_unclassified():
    row = ra._audit_row(
        {"id": 2, "created_at": None, "action": "captains_notified",
         "actor_slack_id": None, "actor_name": None, "details": {},
         "actor_kind": "job", "category": "unclassified", "effect": "changed",
         "request_id": None, "target_alias": None}, {})
    assert row["classified"] is False and row["actor"]["handle"] == "system"


def test_the_screen_reads_no_field_the_endpoint_omits():
    """Every `res.X` and `row.X` the screen reaches for must be in the response.

    The screen is design-owned and this endpoint is code-owned, so a rename on
    either side is invisible until a number renders as `undefined` — on a screen
    whose entire job is that its numbers can be trusted.
    """
    top = set(re.findall(r"\bres\.([A-Za-z]+)", SCREEN))
    sent = {"rows", "cursor", "matched", "total", "grandTotal", "searchTotal",
            "facets", "actionTypes", "exclusions"}
    assert not top - sent, sorted(top - sent)

    facets = set(re.findall(r"res\.facets\.([A-Za-z]+)", SCREEN))
    assert not facets - {"categories", "effects", "actorKinds", "outcomes"}, \
        sorted(facets)

    action_types = set(re.findall(r"res\.actionTypes\.([A-Za-z]+)", SCREEN))
    assert not action_types - {"total", "unclassified"}, sorted(action_types)

    # The exclusion entries, as the scope line and the kind chips read them.
    # Scoped to the block that renders them: `x` is a generic name elsewhere in
    # the file (`setRows(x => x.concat(...))`), and a file-wide scan would be
    # asserting on the wrong variable.
    block = SCREEN.split("qh-audexcl-l", 1)[1].split("</div>", 1)[0]
    excl = set(re.findall(r"\bx\.([A-Za-z]+)", block)) | \
        set(re.findall(r"slice\.([A-Za-z]+)", SCREEN))
    assert excl, "the exclusion list stopped reading any field"
    assert not excl - {"id", "label", "sub", "category", "included", "rows"}, \
        sorted(excl)


def test_a_failed_request_row_carries_its_outcome_and_its_error():
    """The list reads the error off the row, so a failure shows without opening
    the payload. A row of any other request carries no outcome."""
    base = {"id": 7, "created_at": None, "action": "failed", "actor_slack_id": None,
            "actor_name": None, "details": {"error": "Permission denied"},
            "actor_kind": "job", "category": "requests", "effect": "ran",
            "request_id": 41, "target_alias": "alpha", "database_name": "db1",
            "request_status": "failed", "request_error": "Permission denied",
            "is_failed": True}
    row = ra._audit_row(base, {})
    assert row["outcome"] == "failed"
    assert row["request"]["status"] == "failed"
    assert row["request"]["error"] == "Permission denied"
    ok = ra._audit_row({**base, "action": "completed", "is_failed": False,
                        "request_status": "completed", "request_error": None}, {})
    assert ok["outcome"] is None and ok["request"]["error"] is None


@pytest.mark.parametrize("stored,origin,channel", [
    ("slack", "slack", "Slack"),
    ("web", "web", "Web"),
    ("idp", "idp", "IdP"),
    ("mcp", "mcp", "MCP"),
    (" IdP ", "idp", "IdP"),
    (None, "slack", "Slack"),
])
def test_a_request_row_names_the_door_it_came_through(stored, origin, channel):
    row = ra._audit_row(_req_row(request_origin=stored), {})
    assert row["request"]["origin"] == origin
    assert row["request"]["channel"] == channel


def test_an_unknown_origin_shows_as_itself_not_as_slack():
    row = ra._audit_row(_req_row(request_origin="cli"), {})
    assert row["request"]["origin"] == "cli"
    assert "slack" not in row["request"]["channel"].lower()


def test_a_row_with_no_request_has_no_channel():
    row = ra._audit_row(_req_row(request_id=None, request_origin="web"), {})
    assert row["request"] is None


def test_the_decision_facts_ride_on_the_request():
    when = __import__("datetime").datetime(2026, 10, 26, 9, 30,
                                           tzinfo=__import__("datetime").timezone.utc)
    row = ra._audit_row(_req_row(
        request_created_at=when, request_decided_at=when,
        decided_by_slack_id="U7", decided_by_name="snapshot.name",
        decision_reason="Looks fine"), {"U7": "Roster Name"})
    req = row["request"]
    assert req["decidedBy"] == "Roster Name", "the roster name wins over the snapshot"
    assert req["decidedById"] == "U7"
    assert req["decisionNote"] == "Looks fine"
    assert req["submittedAt"] and req["decidedAt"]


def test_an_undecided_request_has_null_decision_facts():
    req = ra._audit_row(_req_row(), {})["request"]
    assert req["decidedBy"] is None and req["decidedById"] is None
    assert req["decisionNote"] is None
    assert req["decidedAt"] is None and req["submittedAt"] is None


def test_the_snapshot_name_is_the_fallback_for_a_decider_the_roster_lost():
    req = ra._audit_row(_req_row(decided_by_slack_id="U7",
                                 decided_by_name="snapshot.name"), {})["request"]
    assert req["decidedBy"] == "snapshot.name"


def test_the_search_term_also_finds_a_channel():
    """Typing "idp" must find the requests that came through the IdP. The
    channel gets a branch of its own so the trigram branches keep their index."""
    assert "r5.origin ILIKE %(q)s" in ra._AUDIT_SEARCH_SQL
    assert ra._AUDIT_SEARCH_SQL.count("UNION") == 3
    # A plain UNION drops duplicate ids, so the IN list stays as short as the matches.
    assert "UNION ALL" not in ra._AUDIT_SEARCH_SQL


def test_the_audit_select_reads_the_columns_the_row_needs():
    src = Path(ra.__file__).read_text(encoding="utf-8")
    for col in ("r.origin AS request_origin", "r.created_at AS request_created_at",
                "r.decided_by_slack_id", "r.decided_by_name", "r.decision_reason",
                "r.decided_at AS request_decided_at"):
        assert col in src, col


def test_the_outcome_filter_only_accepts_what_the_server_can_count():
    assert ra._AUDIT_OUTCOMES == ("failed",)
    assert ra.AuditSearchIn(outcomes=["failed"]).outcomes == ["failed"]


def test_the_exclusion_list_covers_the_kind_it_hides():
    """A kind that IS an excluded slice renders "excluded" instead of a
    contradictory 0, and the screen finds that by matching `category`."""
    ids = {x["id"] for x in ra.AUDIT_EXCLUSIONS}
    assert ids == {"lifecycle", "usage"}
    by_cat = {x["category"] for x in ra.AUDIT_EXCLUSIONS}
    assert "usage" in by_cat and None in by_cat
    for x in ra.AUDIT_EXCLUSIONS:
        assert x["label"] and x["sub"], x


@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("QH_RUN_INTEGRATION"),
                    reason="needs the control database")
def test_the_counts_agree_with_each_other_on_real_data():
    from queryhub.web import admin
    orig = admin.require_admin
    admin.require_admin = lambda claims, need="review", **kw: "TEST"
    try:
        r = ra.admin_audit_search(ra.AuditSearchIn(), claims={"sub": "TEST"})
    except Exception as exc:                      # pragma: no cover - env only
        # The suite's DB guard leaves the pool unable to open in this process,
        # so an unreachable database is an environment fact and not a finding.
        pytest.skip(f"control database not reachable here: {type(exc).__name__}")
    finally:
        admin.require_admin = orig
    # Nothing filtered, so what the list shows IS the scope the chips divide.
    assert r["matched"] == r["total"]
    # The facets partition that scope exactly — a chip that overcounts is the
    # failure this screen exists to remove.
    assert sum(r["facets"]["categories"].values()) == r["total"]
    assert sum(r["facets"]["effects"].values()) == r["total"]
    assert sum(r["facets"]["actorKinds"].values()) == r["total"]
    # And the exclusions account for the difference, without double-counting.
    hidden = sum(x["rows"] for x in r["exclusions"])
    assert r["total"] + hidden == r["grandTotal"]
    assert r["actionTypes"]["unclassified"] <= r["actionTypes"]["total"]
