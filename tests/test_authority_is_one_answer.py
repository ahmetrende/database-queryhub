"""Authority must give the same answer whichever door you come through.

Three separate near-misses this week had one shape: a rule read one model or one
table while a screen, a command or a mirror read another. None of them failed
loudly, because the side that was wrong answered "nothing" rather than raising.
These pin the seams that were closed.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "queryhub"


def _src(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_the_kill_switch_needs_a_super_admin_on_both_doors():
    """The most consequential command in the product had two privilege levels:
    the Slack dispatcher asks for an admin, the web route asks for a super. A
    scoped DBA admin could halt every target from Slack and be refused for the
    same act on the web."""
    slack = _src("slack_app/subcommands.py")
    body = slack.split("def _handle_kill", 1)[1].split("\ndef ", 1)[0]
    assert "is_super_admin" in body, "the Slack kill switch dropped its guard"
    web = _src("web/routes_admin.py")
    kill = web.split("def set_kill", 1)[1].split("\ndef ", 1)[0]
    assert 'require_admin(claims, "access")' in kill


def test_grant_authority_reads_the_model_that_is_switched_on():
    """`granter` sat in the new model's vocabulary while `admins.can_grant`
    decided everything, so the Roles screen displayed an authority that decided
    nothing."""
    g = _src("grants.py")
    assert "use_v2()" in g, "authz stopped consulting the model switch"
    assert "_authz_v2" in g and "_authz_legacy" in g


def test_no_role_in_the_vocabulary_is_unenforced():
    """A role nothing checks reads as protection that is not there. `importer`
    was removed for exactly this; anything added back has to be enforced."""
    roles = re.search(r'_ROLES = \((.*?)\)', _src("web/routes_admin.py"), re.S)
    assert roles
    names = set(re.findall(r'"(\w+)"', roles.group(1)))
    assert names == {"approver", "granter", "admin"}
    for word in ("not yet enforced", "nothing reads them"):
        assert word not in _src("auth_events.py"), (
            f"auth_events still hedges with {word!r} — either the role is "
            "enforced and the wording should say so, or it should not exist")


def test_the_expiry_scanner_looks_at_every_grant_table():
    """The pod cutover writes grants straight to `access_grant`, which has no
    legacy row. Before this branch existed the scanner could not have warned
    about one even if it had wanted to."""
    e = _src("grant_expiry.py")
    for table in ("user_target_grants", "team_target_grants", "access_grant"):
        assert table in e, f"{table} is not scanned for expiries"
    # Native rows only: the mirror copies legacy grants and auto-approve windows
    # into the same table, and warning off both would DM twice — or, for a
    # window, say something untrue about what is being lost.
    assert "mirrored_from IS NULL" in e


def test_a_scoped_legacy_admin_would_be_mirrored_wider_than_it_is():
    """LATENT, and pinned so it cannot become live in silence.

    Migration 109 projects every enabled `admins` row as `all_teams` and
    `all_targets` TRUE, because the schema forbids a scoped `admin`. No live
    admin carries a team or target scope, so nobody has been widened — but the
    first one who does would be, quietly. If this test starts failing, the
    mirror needs to map that person to `approver` with their scope instead.
    """
    mig = (ROOT / "migrations" / "109_mirror_legacy_writes.sql").read_text(
        encoding="utf-8")
    assert "SELECT v_pid, r.role, TRUE, TRUE," in mig, (
        "the mirror's admin projection changed — re-read whether a scoped "
        "legacy admin is still being widened to fleet-wide")


def test_excluding_a_target_from_metrics_never_touches_the_audit_trail():
    """Keeping administration out of a chart and hiding it from an auditor are
    different acts. The audit feed reads `audit_log` directly for this reason."""
    mig = (ROOT / "migrations" / "121_report_excluded_targets.sql").read_text(
        encoding="utf-8")
    assert "requests_reportable" in mig
    assert "audit_log" not in mig.split("CREATE OR REPLACE VIEW", 1)[1]
    web = _src("web/routes_admin.py")
    search = web.split("def admin_audit_search", 1)[1].split("\ndef ", 1)[0]
    # The docstring NAMES the reportable view to say it is not used, so match on
    # the query and not on the word.
    assert "FROM audit_log_reportable" not in search
    assert "FROM audit_log al" in _src("web/routes_admin.py")
