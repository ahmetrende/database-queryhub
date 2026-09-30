-- How far an archive reaches, as the archive itself reports it.
--
-- An Athena target is an archive: rows that left a production database for
-- object storage. A query on it is only as good as the archive is complete, and
-- nothing in the query can tell the approver how far that is. The pipeline that
-- writes the archive knows, and states it in a small JSON marker it writes with
-- a single PutObject:
--
--   covered_through  every row up to this moment is in the archive and visible
--   computed_at      when the marker was written; it proves the pipeline is
--                    alive, and can advance while covered_through does not
--   known_gaps       optional list of {from, to}: holes whose bounds are known
--
-- The hourly catalog refresh reads the marker (engine_config.freshness_marker,
-- an s3:// URI) and stores its verdict here. The submit path reads the stored
-- verdict and adds one sentence to the approver's hint: "Archive complete up to
-- <date> UTC", the known gaps, or "Archive coverage unknown". It is read hourly,
-- not per submit: the marker changes once a night, and a stored verdict gives
-- every submit in the hour the same answer instead of one that depends on a
-- GetObject succeeding at that moment.
--
-- The column holds a VERDICT, not the marker. The marker is untrusted input: a
-- field this code does not know is ignored, and a known_gaps it cannot read
-- makes the verdict "unknown", never "complete". Storing the raw object would
-- move that judgement to every reader.
--
-- Staleness is NOT stored. "The marker has not been updated for N hours" is
-- worked out from computed_at when the hint is built for a submission, against
-- athena_freshness_stale_hours, so a verdict read a day ago still reports its
-- age correctly and the threshold takes effect without a re-read.
--
-- NULL for every other engine, and for an archive target that names no marker.
ALTER TABLE target_servers ADD COLUMN IF NOT EXISTS archive_freshness JSONB;

COMMENT ON COLUMN target_servers.archive_freshness IS
  $$Athena only: the last verdict read from the archive's freshness marker (engine_config.freshness_marker) by the hourly catalog refresh. {state: complete|complete_with_gaps|unknown, covered_through, computed_at, known_gaps, reason, read_at}, timestamps as ISO-8601 UTC. NULL = no marker configured, or not read yet.$$;

INSERT INTO bot_config (key, value, description) VALUES
  ('athena_freshness_stale_hours', '36',
   'Hours after an archive''s freshness marker was written (its computed_at) before the approver''s hint warns that the marker has not been updated. 36 = a nightly writer plus a margin for a late run.')
ON CONFLICT (key) DO NOTHING;
