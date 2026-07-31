-- Correct the audit_log_reportable semantic.
--
-- Migration 032 dropped audit rows whose `actor_slack_id` was in
-- `report_excluded_users`. That was overzealous: an excluded user
-- can still approve OTHER people's real requests, and those approval
-- actions SHOULD appear in admin reports (p_metrics_admin_workload)
-- because they reflect real DBA work on real (non-excluded) traffic.
--
-- The right rule: an audit row is dropped iff the *request it acted on*
-- is dropped (i.e. the request is NOT in requests_reportable).
-- Actor identity alone never excludes an action.
--
-- request_ratings_reportable: tighten to the same shape — drop the
-- per-rater check since a rating's slack_user_id is always the
-- request's requester anyway, so the request-level filter is
-- sufficient and avoids the double-redundancy.

CREATE OR REPLACE VIEW audit_log_reportable AS
SELECT al.*
  FROM audit_log al
 WHERE al.request_id IS NULL
    OR EXISTS (
        SELECT 1 FROM requests_reportable r
         WHERE r.id = al.request_id
    );

COMMENT ON VIEW audit_log_reportable IS
$$`audit_log` minus rows whose linked `request_id` is itself filtered out by `requests_reportable`. Rows with `request_id IS NULL` (system-level audit) stay. So an excluded user's approvals of OTHER people's real requests are kept — only actions on the excluded user's own requests vanish from reports.$$;

CREATE OR REPLACE VIEW request_ratings_reportable AS
SELECT rr.*
  FROM request_ratings rr
 WHERE EXISTS (
     SELECT 1 FROM requests_reportable r WHERE r.id = rr.request_id
 );

COMMENT ON VIEW request_ratings_reportable IS
$$`request_ratings` minus ratings whose underlying request is filtered out by `requests_reportable`. A rating's slack_user_id is the request's requester by construction, so the request-level filter is sufficient.$$;
