"""The transcription from the old authorization tables into the new ones.

Two properties carry the whole migration and both failed on the first real run,
which is why they are pinned rather than assumed.

Idempotency, because the copy is not a one-off: it runs now, the old tables stay
live for weeks, and it runs again minutes before the cutover. The first version
re-inserted the three revoked user grants on every pass — the existence check
only looked at live rows, so it could never find a revoked one. Three rows per
run, in the table that decides who may reach production, growing quietly.

And behaviour preservation, because the two models do not mean the same thing by
the same row. An auto-approve window waives the wait; read as a grant it hands
out access nobody gave. Three of the live windows are fleet-wide.

These tests read the script and its migration rather than a database: what they
guard is that the reasoning stays written down where the next person will look.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "scripts" / "copy_access_model.py").read_text(encoding="utf-8")
MIG106 = (ROOT / "migrations" / "106_access_model_fixes.sql").read_text(encoding="utf-8")


def _func(name: str) -> str:
    m = re.search(rf"\ndef {name}\(.*?(?=\ndef |\nclass |\Z)", SRC, re.S)
    assert m, f"{name} is gone"
    return m.group(0)


# --- idempotency ------------------------------------------------------------


def test_a_revoked_grant_is_matched_on_more_than_its_scope():
    """The live-grant unique index does not cover revoked rows, so a scope-only
    lookup can never find one and re-inserts it every run."""
    body = _func("_grant_exists")
    assert "revoked_at is None" in body
    assert "valid_from" in body and "revoked_at = %(revoked_at)s" in body


def test_both_grant_sources_check_existence_before_inserting():
    body = _func("copy_grants")
    for source in ("team_target_grants", "user_target_grants"):
        assert source in body
    # every insert is guarded by a lookup that knows about revocation
    assert body.count("_grant_exists(") >= 3
    assert body.count("revoked_at=g[\"revoked_at\"]") >= 2


def test_principals_teams_members_and_settings_all_check_first():
    for name, marker in [("copy_principals", "if sid in have"),
                         ("copy_teams", "SELECT 1 FROM team_member"),
                         ("copy_roles", "SELECT 1 FROM role_assignment"),
                         ("copy_settings", "SELECT 1 FROM principal_setting")]:
        assert marker in _func(name), name


def test_a_dry_run_leaves_by_the_rollback_door():
    """`db.transaction()` commits on a clean exit, so a dry run cannot simply
    return — it has to raise."""
    assert "raise _Rollback(totals)" in SRC
    assert "class _Rollback" in SRC


def test_the_audit_row_is_written_inside_the_transaction():
    """`audit.log` opens its own connection, so its row would survive a dry
    run's rollback and claim a copy that never happened."""
    assert "audit.log_in(cur" in SRC
    assert re.search(r"(?<!log_)audit\.log\(", SRC) is None


# --- behaviour preservation -------------------------------------------------


def test_an_auto_approve_row_is_copied_as_a_waiver_not_a_grant():
    body = _func("copy_grants")
    m = re.search(r"auto_approve_grants -> waivers.*?auto_approve=True", body, re.S)
    assert m, "the auto-approve copy no longer says what it is"
    assert "auto_approve=True" in body


def test_only_live_auto_windows_are_copied():
    """An expired waiver is a permission that no longer applies, and the same
    person often holds a series of them on one database — identical scopes that
    the live-grant index admits exactly one of."""
    body = _func("copy_grants")
    assert "expires_at IS NULL OR expires_at > now()" in body
    assert "ORDER BY expires_at DESC NULLS FIRST" in body  # widest window wins


def test_the_waiver_rule_is_recorded_in_the_database_itself():
    """A rule the columns cannot show has to be discoverable from the schema,
    not only from a script somebody may not read."""
    assert "COMMENT ON COLUMN access_grant.auto_approve" in MIG106
    assert "does NOT grant" in MIG106


def _insert_call(body: str, marker: str) -> str:
    """The _insert_grant(...) call that carries `marker` in its reason."""
    m = re.search(r"_insert_grant\(cur,(?:[^()]|\([^()]*\))*?"
                  + re.escape(marker) + r"(?:[^()]|\([^()]*\))*?\)", body, re.S)
    assert m, f"no insert carrying {marker!r}"
    return m.group(0)


def test_a_user_grant_replaces_the_team_rather_than_adding_to_it():
    """`merge_with_team` false — the default — is what `user_target_grants`
    means today: the user's row stands in for the team's, which is how a row
    can NARROW what a team allows."""
    call = _insert_call(_func("copy_grants"), "carried from user_target_grants")
    assert "merge_with_team" not in call


def test_a_waiver_does_not_suppress_the_team_grant_underneath_it():
    """The trap in folding auto-approve into the grant table. A waiver is a
    principal row, and principal rows beat team rows — so a waiver written with
    the default `merge_with_team=false` would silently delete the team access of
    everyone who has an auto-approve window. It has to merge."""
    body = _func("copy_grants")
    call = re.search(r"_insert_grant\(cur, merge_with_team=True.*?\*\*k\)", body, re.S)
    assert call, "the waiver insert no longer merges with the team"
    assert "auto_approve=True" in body


def test_the_bypass_flag_becomes_an_explicit_fleet_wide_grant():
    body = _func("copy_grants")
    assert "bypass_team_grants" in body
    assert 'tier="ddl"' in body


def test_an_admins_tier_ceiling_survives_as_an_approval_ceiling():
    """One of the two admins may approve only read requests while still
    reaching every target. 105 made that inexpressible; 106 fixed it."""
    assert "role_assignment_admin_is_fleet_wide" in MIG106
    assert "any_tier" not in MIG106.split("role_assignment_admin_is_fleet_wide")[1][:200]
    assert "max_tier" in _func("copy_roles")


def test_can_grant_becomes_its_own_role():
    assert '"granter"' in _func("copy_roles")


def test_enabled_is_carried_across_rather_than_defaulted():
    """`principal.enabled` defaults FALSE so a directory sync cannot switch
    anyone on. A transcription is not a sync, and defaulting here would lock
    everybody out at the cutover."""
    body = _func("copy_principals")
    assert "enabled" in body and "r[\"enabled\"]" in body


def test_a_grant_is_never_dropped_for_want_of_an_attribution():
    """Two `granted_by` values name nobody who still exists."""
    body = _func("copy_grants")
    assert "unattributed" in body
    assert "attribution(" in body


# --- the verification step --------------------------------------------------


def test_verify_compares_every_table_it_wrote():
    body = _func("verify")
    for table in ("principal", "team", "team_member", "access_grant",
                  "role_assignment", "principal_setting"):
        assert table in body, table


def test_verify_counts_exploded_rows_not_source_rows():
    """One old grant naming two databases becomes two rows. Comparing row
    counts would report a mismatch that is the migration working."""
    assert "cardinality(allowed_databases)" in _func("verify")


def test_verify_counts_distinct_scopes_for_waivers():
    """Overlapping waivers on one database collapse into the widest."""
    body = _func("verify")
    assert "GROUP BY slack_user_id, target_server_id, database_name, max_tier" in body


def test_verify_notices_a_grant_with_no_subject():
    assert "grants with no subject" in _func("verify")


# --- the gate itself, after the mirror and a second team source --------------
#
# `--verify` is what stands between a green diff and turning `access_model_v2`
# on, so the two ways it went wrong on 2026-09-08 matter more than most bugs:
# a gate that reports problems it does not have stops being read.


def test_the_team_counts_are_scoped_to_what_this_script_owns():
    """`team` also carries structures written by `scripts/import_teams.py`
    under its own source. Counting those against the legacy table made the
    gate report 19 vs 6 with nothing wrong, the first time an org import ran.
    Scoped to `manual`, which is exactly how the migration-109 mirror scopes
    itself."""
    i = SRC.index('("team", "SELECT count(*) FROM team ')
    block = SRC[i:i + 700]
    assert "source = 'manual'" in block
    assert block.count("source = 'manual'") >= 2, "both team checks"


def test_drift_is_measured_by_comparing_sets_not_clocks():
    """It used to ask whether any legacy grant was TIMESTAMPED after the newest
    row in `access_grant` — the right approximation while the copy was the only
    writer. The mirror propagates inside the same transaction, so the two
    timestamps land microseconds apart in an order nothing guarantees, and two
    already-identical grants were flagged as drift.

    A clock cannot answer this. The sets can, exactly and in one query."""
    assert "EXCEPT SELECT * FROM new" in SRC
    assert "EXCEPT SELECT * FROM old" in SRC
    assert "max(created_at) FROM access_grant" not in SRC


def test_the_drift_check_says_what_would_have_caused_it():
    """The only thing that can make the two sides differ now is the mirror
    being off or failing. Naming that is the difference between a number and
    an instruction."""
    # The sentence is split across f-string lines in the source; match the
    # half that is not.
    assert "is off or behind" in SRC


def test_drift_ignores_grants_that_have_run_out():
    """Both sides filter expiry, or a lapsed grant present in one model and
    tidied from the other reads as drift forever."""
    i = SRC.index("WITH old AS (")
    block = SRC[i:i + 1600]
    assert "u.expires_at IS NULL OR u.expires_at > now()" in block
    assert "g.valid_until IS NULL OR g.valid_until > now()" in block


# --- and the gate after the two models were allowed to diverge ---------------
#
# The pod cutover (2026-09-08) moved team structure into the new model for
# good: the legacy team tables are empty and every team grant since is written
# straight to `access_grant`. Both remaining count checks were still phrased as
# "everything on the new side came from the old side", which stopped being true
# that day. A gate reporting drift it cannot have is a gate that gets ignored,
# and the next real drift goes with it.


def test_the_team_grant_count_is_scoped_to_the_mirrors_own_rows():
    """`access_grant` now holds team grants with no legacy counterpart -- one
    per pod, and more with every pod that gains a database. Counted against an
    empty `team_target_grants` they read as 72 rows of drift.

    `mirrored_from` is what says a row projects a legacy row, which makes it
    the only honest scope for a check that compares the two models. Identical
    in shape to the `source = 'manual'` scoping the team checks already use."""
    i = SRC.index('("team grants",')
    block = SRC[i:i + 900]
    assert "mirrored_from = 'team_target_grants'" in block


def test_the_user_grant_count_selects_on_the_marker_not_the_prose():
    """It used to match `reason = 'carried from user_target_grants'`. The copy
    writes that string; the mirror writes "mirrored from ...". So every grant
    made after the cutover was invisible to the count while revoked rows still
    counted, and the gate said 82 vs 80 about two sets that agreed row for row
    -- which is how the count was caught rather than the data.

    A reason is prose written for a person to read. It is not a predicate."""
    # The prose still quotes the old predicate, and the copy still WRITES that
    # reason -- correctly, it is what a person reads on the row. So assert on
    # the query, not on the file.
    sql = SRC[SRC.index("    got = one(", SRC.index("# user grants:")):]
    sql = sql[:sql.index("    want = one(")]
    assert "mirrored_from = 'user_target_grants'" in sql
    assert "reason" not in sql


def test_both_count_checks_compare_live_rows_on_both_sides():
    """The fourth appearance of one fault in a day: a revoked row is not a
    grant, it is the record that one ended, and counting it on the side that
    kept it reports drift when both sides are right. The waiver check learned
    this first; these two learned it last."""
    for anchor in ('("team grants",', "# user grants:"):
        block = SRC[SRC.index(anchor):][:1500]
        assert "revoked_at IS NULL AND NOT is_deleted" in block, anchor
        assert "valid_until IS NULL OR valid_until > now()" in block, anchor
        assert "revoked_at IS NULL " in block, anchor       # the legacy side
        assert "expires_at IS NULL OR expires_at > now()" in block, anchor
