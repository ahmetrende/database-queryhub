"""The audit screen fails towards showing too much.

Reported: a requester withdrew request #7368 and nothing about it appeared on
the web audit screen -- only their login. The row was there. `cancellation.
withdraw` had written it. The screen filtered it out.

It filtered with an ALLOWLIST of 37 action names, and this table holds 132
distinct actions: 110 of them were invisible. Not just `withdrawn` --
`result_unmasked`, which is a super-admin reading unmasked personal data;
`pii_masking_exempted`, 273 rows of masking being switched off for a column;
`security_config_changed`; every auto-approve grant. Each was added to the
product over months, and each stayed absent until somebody thought to add its
name to a dictionary in a mapping module. Nobody ever did.

This screen had already been caught hiding things once: it read
`audit_log_reportable`, which drops rows belonging to the operator's own
requests, so the most privileged activity on the fleet was the one thing the
audit trail would not show. Twice is a shape.

So the filter is now a denylist of per-request lifecycle noise -- 17,657 of the
25,890 rows are "submitted / started / completed / failed", which the queue and
history screens show request by request -- and everything else is visible by
default. Visible action types went from 37 to 123.
"""
import inspect

from queryhub.web import mapping, routes_admin


def test_the_filter_excludes_rather_than_admits():
    """The property, not the list: an action nobody has classified must appear
    anyway. An allowlist makes silence the default, and silence in an audit
    view is indistinguishable from nothing having happened."""
    src = inspect.getsource(routes_admin.admin_audit)
    assert 'NOT (al.action = ANY(%s))' in src
    assert "mapping.AUDIT_EXCLUDE" in src


def test_a_brand_new_action_is_visible_and_categorised():
    assert "unheard_of_action" not in mapping.AUDIT_EXCLUDE
    assert mapping.audit_kind("unheard_of_action") == "scope"


def test_the_withdrawal_that_prompted_this_is_shown():
    assert "withdrawn" not in mapping.AUDIT_EXCLUDE
    assert mapping.audit_kind("withdrawn") == "reject"
    assert mapping.audit_kind("cancelled_running") == "reject"


def test_the_events_worth_an_auditors_attention_are_shown():
    """Each of these was hidden. Each changes what somebody can see or do."""
    for action, kind in (("result_unmasked", "access"),
                         ("pii_masking_exempted", "grant"),
                         ("auto_approve_granted", "auto"),
                         ("kill_switch_set", "kill"),
                         ("pod_access_grant", "grant"),
                         ("team_grants_revoked", "grant"),
                         ("web_result_downloaded", "access"),
                         ("effective_access_viewed", "access")):
        assert action not in mapping.AUDIT_EXCLUDE, action
        assert mapping.audit_kind(action) == kind, action


def test_only_per_request_lifecycle_is_excluded():
    """The exclusions have to earn it: each is shown request-by-request on the
    queue and history screens, and together they are nine tenths of the table.
    Anything that is not that belongs on the audit trail."""
    assert mapping.AUDIT_EXCLUDE == frozenset({
        "submitted", "execution_started", "completed", "failed",
        "import_submitted", "import_execution_started", "import_completed",
        "cancel_requested", "scheduled_dispatched"})


def test_the_explicit_mapping_still_wins_over_a_keyword():
    """`escalated_to_dba` contains none of the keywords and is mapped by hand;
    `auto_approved` contains "auto" but is an approval, not an auto-approve
    grant, and the dictionary says so."""
    assert mapping.audit_kind("auto_approved") == "approve"
    assert mapping.audit_kind("escalated_to_dba") == "reject"


def test_no_category_is_invented_that_the_screen_cannot_draw():
    """The screen draws seven kinds. An eighth needs a chip and a colour,
    which is a design change, so connection and config work lands in `scope`
    -- where the screen already puts what it does not recognise."""
    drawable = {"approve", "reject", "changes", "grant", "auto", "scope",
                "access", "kill"}
    for action in list(mapping.AUDIT_KIND) + [
            "target_disabled", "config_set", "migration_resealed",
            "teams_imported", "requester_added", "schema_refreshed"]:
        assert mapping.audit_kind(action) in drawable, action
