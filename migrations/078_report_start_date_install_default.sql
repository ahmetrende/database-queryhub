-- 078: stop shipping the author's pilot date as the reporting start date.
--
-- Migration 035 seeded `report_start_date` with the literal '2026-05-01' — the
-- date THIS installation's pilot began — and the metrics views fall back to the
-- same literal. On any other install that is a meaningless boundary: the
-- dashboard silently reports "since 2026-05-01" for a system that started
-- collecting data later, and every "since launch" figure is wrong.
--
-- Fix without disturbing an existing deployment:
--   * If the stored value is still exactly the seeded literal AND this database
--     has no request older than it, the value was never meaningful here — set it
--     to the date the control plane was actually created.
--   * Otherwise leave it alone: either the operator chose a date, or the
--     seeded date really is when this install started collecting.
--
-- Idempotent: re-running changes nothing once the value differs from the seed.

DO $$
DECLARE
    seeded   CONSTANT date := DATE '2026-05-01';
    current_val text;
    oldest      date;
    install_day date;
BEGIN
    SELECT value INTO current_val
      FROM bot_config WHERE key = 'report_start_date';
    IF current_val IS NULL OR current_val::date <> seeded THEN
        RETURN;                       -- operator-chosen or already migrated
    END IF;

    SELECT min(created_at)::date INTO oldest FROM requests;
    IF oldest IS NOT NULL AND oldest < seeded THEN
        RETURN;                       -- the seed is genuinely this install's start
    END IF;

    -- Earliest signal we have for "when this install began": the first request,
    -- else the first migration ledger row, else today.
    SELECT LEAST(
             COALESCE(oldest, DATE '9999-12-31'),
             COALESCE((SELECT min(applied_at)::date FROM schema_migrations),
                      DATE '9999-12-31'),
             CURRENT_DATE)
      INTO install_day;

    UPDATE bot_config
       SET value = install_day::text,
           description = 'Reporting window start (YYYY-MM-DD). Defaults to when '
                         'this installation was created; set it to the date you '
                         'want "since launch" metrics measured from.'
     WHERE key = 'report_start_date';

    RAISE NOTICE 'report_start_date: % -> % (was the shipped default)',
                 current_val, install_day;
END $$;
