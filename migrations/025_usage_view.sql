-- Daily usage overview + a generic milestone-annotation table so dashboards
-- can overlay "what changed on this day" markers (go-live, access cutover,
-- migration, etc.) on top of the volume curves.
--
-- The existing p_metrics_volume_* views give status / DAU counts. This view
-- adds tier mix, escalations, latency, and row throughput in one place, so
-- a single SELECT answers "how is the bot being used day-to-day?".

-- 1. Annotation table -------------------------------------------------------

CREATE TABLE IF NOT EXISTS metric_annotations (
    id          SERIAL      PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL,
    label       TEXT        NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (occurred_at, label)
);

CREATE INDEX IF NOT EXISTS idx_metric_annotations_occurred
    ON metric_annotations (occurred_at);

COMMENT ON TABLE metric_annotations IS
$$Free-form milestones overlaid on product-metric dashboards. One row per event (go-live, cutover, incident, config change). Joined into p_metrics_usage_daily by date; surface in any custom dashboard by date_trunc('day', occurred_at).$$;

COMMENT ON COLUMN metric_annotations.occurred_at IS
$$Exact moment of the event with timezone. The dashboard layer typically truncates to day for matching against daily aggregates.$$;

COMMENT ON COLUMN metric_annotations.label IS
$$Short marker text (under ~40 chars). Shown on the chart.$$;

COMMENT ON COLUMN metric_annotations.description IS
$$Optional longer note — context, owner, link to runbook / postmortem.$$;

-- 2. Seed the known pilot milestones ---------------------------------------

INSERT INTO metric_annotations (occurred_at, label, description)
SELECT '2026-05-05 17:00:00+03'::timestamptz,
       'Pilot go-live',
       'Slack bot opened to the first wave of pilot users.'
WHERE NOT EXISTS (
    SELECT 1 FROM metric_annotations
     WHERE occurred_at = '2026-05-05 17:00:00+03'::timestamptz
       AND label = 'Pilot go-live'
);

INSERT INTO metric_annotations (occurred_at, label, description)
SELECT '2026-05-08 16:00:00+03'::timestamptz,
       'Direct DB access disabled',
       'Developer-level direct DB access revoked. All ad-hoc queries must go through /sql from this point on.'
WHERE NOT EXISTS (
    SELECT 1 FROM metric_annotations
     WHERE occurred_at = '2026-05-08 16:00:00+03'::timestamptz
       AND label = 'Direct DB access disabled'
);

-- 3. Usage overview view ---------------------------------------------------
--
-- Daily, all-time. Each row carries the per-day request totals, tier mix,
-- escalation count, latency stats, row throughput, and (if any) annotations
-- that happened on that day.

CREATE OR REPLACE VIEW p_metrics_usage_daily AS
WITH daily AS (
    SELECT
        date_trunc('day', created_at)::date              AS day,
        count(*)                                         AS submitted,
        count(*) FILTER (WHERE status = 'completed')     AS completed,
        count(*) FILTER (WHERE status = 'failed')        AS failed,
        count(*) FILTER (WHERE status = 'rejected')      AS rejected,
        count(*) FILTER (WHERE status = 'cancelled')     AS cancelled,
        count(*) FILTER (WHERE status = 'awaiting_dba_manual') AS awaiting_dba,
        count(*) FILTER (WHERE scheduled_for IS NOT NULL) AS scheduled,
        count(DISTINCT requester_slack_id)               AS active_users,
        count(DISTINCT target_server_id)                 AS targets_touched,
        sum(coalesce(row_count, 0))                      AS rows_returned,
        round(avg(extract(epoch FROM (completed_at - executed_at)))
              FILTER (WHERE completed_at IS NOT NULL
                        AND executed_at  IS NOT NULL)::numeric, 2)
                                                         AS avg_exec_sec,
        round((percentile_cont(0.95) WITHIN GROUP (
                  ORDER BY extract(epoch FROM (completed_at - executed_at))
              ) FILTER (WHERE completed_at IS NOT NULL
                          AND executed_at  IS NOT NULL))::numeric, 2)
                                                         AS p95_exec_sec,
        round(avg(extract(epoch FROM (decided_at - created_at)))
              FILTER (WHERE decided_at IS NOT NULL)::numeric, 2)
                                                         AS avg_approval_sec
      FROM requests
     GROUP BY 1
),
ann AS (
    SELECT date_trunc('day', occurred_at)::date          AS day,
           string_agg(label, ' | ' ORDER BY occurred_at) AS annotations
      FROM metric_annotations
     GROUP BY 1
)
SELECT d.day,
       d.submitted,
       d.completed,
       d.failed,
       d.rejected,
       d.cancelled,
       d.awaiting_dba,
       d.scheduled,
       d.active_users,
       d.targets_touched,
       d.rows_returned,
       d.avg_exec_sec,
       d.p95_exec_sec,
       d.avg_approval_sec,
       a.annotations
  FROM daily d
  LEFT JOIN ann a USING (day)
 ORDER BY d.day DESC;

COMMENT ON VIEW p_metrics_usage_daily IS
$$Daily usage overview, all-time. Per day: submission + terminal-state counts (including awaiting_dba_manual + scheduled), active users, distinct targets touched, total rows returned, mean / p95 execution latency, mean approval latency, and any metric_annotations that occurred that day. Designed as the single primary feed for usage dashboards.$$;
