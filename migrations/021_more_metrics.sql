-- More p_metrics_* views: per-target heatmap, peak hours (with
-- business-hours awareness), approval-SLA latency.
--
-- Timezone-sensitive views read bot_config.report_timezone so a
-- non-UTC deployment can group by local clock without rewriting SQL.

INSERT INTO bot_config (key, value, description) VALUES
    ('report_timezone', 'Europe/Istanbul',
     'IANA timezone string used by p_metrics_* views for hour-of-day / day-of-week bucketing. Set to your team''s primary timezone for sensible peak-hour reports.')
ON CONFLICT (key) DO NOTHING;

-- Text-config helper (mirrors p_metrics_cfg_num for numeric values).
CREATE OR REPLACE FUNCTION p_metrics_cfg_text(p_key text, p_default text DEFAULT '')
RETURNS text LANGUAGE sql STABLE AS $$
    SELECT COALESCE(NULLIF((SELECT value FROM bot_config WHERE key = p_key), ''),
                    p_default);
$$;


-- ---------- Per-target heat map ----------

-- Surfaces both heavy-use AND dead-weight targets (LEFT JOIN from
-- target_servers — targets with zero requests show as a row with 0s,
-- so the dropdown bloat is visible).

CREATE OR REPLACE VIEW p_metrics_target_heatmap AS
SELECT ts.alias                                                           AS target,
       ts.enabled,
       COALESCE(count(r.id), 0)                                           AS total_requests,
       count(r.id) FILTER (WHERE r.created_at >= NOW() - INTERVAL '7 days')  AS last_7d,
       count(r.id) FILTER (WHERE r.created_at >= NOW() - INTERVAL '30 days') AS last_30d,
       count(DISTINCT r.requester_slack_id)                               AS unique_users,
       count(r.id) FILTER (WHERE r.status = 'completed')                  AS completed,
       count(r.id) FILTER (WHERE r.status = 'failed')                     AS failed,
       count(r.id) FILTER (WHERE r.status = 'rejected')                   AS rejected,
       max(r.created_at)                                                  AS last_request_at
  FROM target_servers ts
  LEFT JOIN requests r ON r.target_server_id = ts.id
 GROUP BY ts.id, ts.alias, ts.enabled
 ORDER BY total_requests DESC, ts.alias;

COMMENT ON VIEW p_metrics_target_heatmap IS
$$Per-target usage. LEFT JOIN — targets with zero requests appear as 0 rows so unused/never-used targets stay visible. Sort by total_requests DESC for heatmap; sort by total_requests ASC to find dead-weight rows.$$;


-- ---------- Peak-hour heatmap (hour-of-day × day-of-week) ----------

-- Bucketed by hour (0-23) and dow (0=Sunday, 6=Saturday) in the
-- report_timezone. 90-day window so the picture is recent.

CREATE OR REPLACE VIEW p_metrics_peak_hours AS
WITH local_ts AS (
    SELECT created_at,
           created_at AT TIME ZONE p_metrics_cfg_text('report_timezone', 'UTC')
               AS local_created_at,
           status
      FROM requests
     WHERE created_at >= NOW() - INTERVAL '90 days'
)
SELECT EXTRACT(dow  FROM local_created_at)::int                       AS dow,
       to_char(local_created_at, 'Dy')                                AS day_name,
       EXTRACT(hour FROM local_created_at)::int                       AS hour,
       count(*)                                                       AS requests,
       count(*) FILTER (WHERE status = 'completed')                   AS completed,
       count(DISTINCT date_trunc('day', local_created_at))            AS days_with_activity
  FROM local_ts
 GROUP BY 1, 2, 3
 ORDER BY dow, hour;

COMMENT ON VIEW p_metrics_peak_hours IS
$$Request density by (day-of-week × hour-of-day) in the local timezone (bot_config.report_timezone). 90-day window. dow: 0=Sun, 1=Mon, ..., 6=Sat. Plot as a heatmap to spot peak windows.$$;


-- ---------- Business hours vs off-hours / weekend ----------

-- Business hours = Mon-Fri 09:00-17:59 in report_timezone.
-- Off-hours = anything else (incl. weekend).

CREATE OR REPLACE VIEW p_metrics_business_vs_offhours AS
WITH classified AS (
    SELECT created_at,
           created_at AT TIME ZONE p_metrics_cfg_text('report_timezone', 'UTC')
               AS local_ts,
           status
      FROM requests
)
SELECT date_trunc('week', created_at)::date                            AS week,
       count(*)                                                        AS total,
       count(*) FILTER (
           WHERE EXTRACT(dow  FROM local_ts) BETWEEN 1 AND 5
             AND EXTRACT(hour FROM local_ts) BETWEEN 9 AND 17)         AS business_hours,
       count(*) FILTER (
           WHERE NOT (EXTRACT(dow  FROM local_ts) BETWEEN 1 AND 5
                  AND EXTRACT(hour FROM local_ts) BETWEEN 9 AND 17))   AS off_hours,
       count(*) FILTER (WHERE EXTRACT(dow FROM local_ts) IN (0, 6))    AS weekend,
       count(*) FILTER (WHERE EXTRACT(dow FROM local_ts) IN (1,2,3,4,5)
                          AND EXTRACT(hour FROM local_ts) >= 18)       AS weekday_evening,
       count(*) FILTER (WHERE EXTRACT(dow FROM local_ts) IN (1,2,3,4,5)
                          AND EXTRACT(hour FROM local_ts) < 9)         AS weekday_early,
       round(100.0 * count(*) FILTER (
           WHERE NOT (EXTRACT(dow  FROM local_ts) BETWEEN 1 AND 5
                  AND EXTRACT(hour FROM local_ts) BETWEEN 9 AND 17))
           / NULLIF(count(*), 0), 1)                                   AS off_hours_pct
  FROM classified
 GROUP BY 1
 ORDER BY 1 DESC;

COMMENT ON VIEW p_metrics_business_vs_offhours IS
$$Weekly split: business-hours (Mon-Fri 09-17 in report_timezone) vs everything else. Surfaces nights-and-weekends usage — useful for SLA / on-call planning.$$;


-- ---------- Approval SLA (latency) ----------

-- Latency = decided_at − created_at, measured per request. p50 / p95 /
-- bucket counts so you can see "median 4 min, but 5% wait > 1 hour".
-- Excludes still-pending requests so the percentiles aren't pulled
-- toward infinity by open queue.

CREATE OR REPLACE VIEW p_metrics_approval_sla AS
SELECT date_trunc('week', created_at)::date                                  AS week,
       count(*)                                                              AS decided,
       round(percentile_cont(0.50) WITHIN GROUP (
           ORDER BY EXTRACT(EPOCH FROM (decided_at - created_at))
       )::numeric, 0)                                                        AS p50_sec,
       round(percentile_cont(0.90) WITHIN GROUP (
           ORDER BY EXTRACT(EPOCH FROM (decided_at - created_at))
       )::numeric, 0)                                                        AS p90_sec,
       round(percentile_cont(0.95) WITHIN GROUP (
           ORDER BY EXTRACT(EPOCH FROM (decided_at - created_at))
       )::numeric, 0)                                                        AS p95_sec,
       count(*) FILTER (WHERE EXTRACT(EPOCH FROM (decided_at - created_at)) <= 300)    AS within_5min,
       count(*) FILTER (WHERE EXTRACT(EPOCH FROM (decided_at - created_at)) <= 1800)   AS within_30min,
       count(*) FILTER (WHERE EXTRACT(EPOCH FROM (decided_at - created_at)) <= 3600)   AS within_1h,
       count(*) FILTER (WHERE EXTRACT(EPOCH FROM (decided_at - created_at)) > 3600)    AS over_1h
  FROM requests
 WHERE decided_at IS NOT NULL
 GROUP BY 1
 ORDER BY 1 DESC;

COMMENT ON VIEW p_metrics_approval_sla IS
$$Weekly approval-latency percentiles (decided_at − created_at) + bucket counts (within 5m / 30m / 1h / over 1h). Excludes still-pending requests. Drives SLA target setting + admin staffing decisions.$$;
