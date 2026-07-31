-- Per-request user rating + optional written feedback. Captured via a
-- follow-up DM (1-5 buttons) that the bot sends after a request reaches
-- a terminal state (completed / failed / rejected / cancelled). One
-- rating per request (request_id is unique). The bot suppresses the
-- prompt for 30 days after a user's most recent rating, so survey
-- fatigue stays bounded.

INSERT INTO bot_config (key, value, description) VALUES
    ('rating_enabled', 'on',
     'When "on", the bot DMs a 1-5 rating prompt after every terminal-state request (completed/failed/rejected/cancelled), suppressed for 30 days after a user''s most recent rating. Set to "off" to disable rating prompts entirely (existing ratings remain).')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS request_ratings (
    id              SERIAL      PRIMARY KEY,
    request_id      INTEGER     NOT NULL UNIQUE
                                REFERENCES requests(id) ON DELETE CASCADE,
    slack_user_id   TEXT        NOT NULL,
    rating          SMALLINT    NOT NULL CHECK (rating BETWEEN 1 AND 5),
    feedback_text   TEXT,
    rated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_request_ratings_user_time
    ON request_ratings (slack_user_id, rated_at DESC);

COMMENT ON TABLE request_ratings IS
$$User-supplied 1-5 rating + optional free-text feedback for a single request. Captured via post-completion DM prompt; one rating per request, locked first-come-first-served. Used for product KPIs (avg rating, response rate, low-rating drilldown).$$;

-- Convenience views for product metrics. Read-only aggregations; safe
-- to expose to anyone with read access on the bot DB.

CREATE OR REPLACE VIEW v_rating_weekly AS
SELECT date_trunc('week', rated_at)::date AS week,
       count(*)                            AS n_ratings,
       round(avg(rating)::numeric, 2)      AS avg_rating,
       count(*) FILTER (WHERE rating <= 2) AS low_count,
       count(*) FILTER (WHERE rating >= 4) AS high_count,
       count(feedback_text)                AS with_feedback
  FROM request_ratings
 GROUP BY 1
 ORDER BY 1 DESC;

COMMENT ON VIEW v_rating_weekly IS
$$Weekly rollup of request_ratings. Columns: week (Monday), n_ratings, avg_rating, low_count (≤2), high_count (≥4), with_feedback.$$;

CREATE OR REPLACE VIEW v_rating_response_rate AS
SELECT date_trunc('week', r.completed_at)::date  AS week,
       count(*)                                  AS terminal_requests,
       count(rr.*)                               AS rated,
       round((count(rr.*)::numeric * 100)
             / NULLIF(count(*), 0), 1)            AS response_pct
  FROM requests r
  LEFT JOIN request_ratings rr ON rr.request_id = r.id
 WHERE r.status IN ('completed','failed','rejected','cancelled')
   AND r.completed_at IS NOT NULL
 GROUP BY 1
 ORDER BY 1 DESC;

COMMENT ON VIEW v_rating_response_rate IS
$$Weekly response rate: of all requests reaching a terminal state, what fraction received a user rating.$$;

CREATE OR REPLACE VIEW v_rating_low_with_feedback AS
SELECT rr.rated_at,
       rr.rating,
       rr.feedback_text,
       r.id                AS request_id,
       r.requester_name,
       r.requester_slack_id,
       r.target_server_id,
       r.status,
       left(r.query, 200)  AS query_preview
  FROM request_ratings rr
  JOIN requests r ON r.id = rr.request_id
 WHERE rr.rating <= 2
 ORDER BY rr.rated_at DESC;

COMMENT ON VIEW v_rating_low_with_feedback IS
$$Drill-down for low ratings (1-2). Includes the request preview so a maintainer can spot patterns (which targets / query shapes correlate with bad UX).$$;
