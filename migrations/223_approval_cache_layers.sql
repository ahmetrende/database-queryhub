-- Approval cache: two more layers next to the exact fingerprint (mig 044).
--
-- Layer 1, canonical form (`query_canonical_fp`): the fingerprint also
-- ignores comments, IN-list length, table alias names, the order and aliases
-- of the outer SELECT list, the order of AND / OR operands and of inner
-- joins, and the outer ORDER BY. See ast_safety.canonical_fingerprint.
-- Switch: `approval_cache_canonical` (on by default).
--
-- Layer 2, column footprint (`query_footprint`): a RO query whose every
-- (table, column, use) earlier completed requests already read, in the same
-- scope, can skip review. See ast_safety.footprint. Switch:
-- `approval_cache_footprint` = off | shadow | on. It starts in `shadow`: the
-- request waits for an admin as before, and `approval_shadow` records the
-- requests that would have approved it, for a later review.
--
-- RO only, like the exact fingerprint. Same requester, target, database and
-- TTL (`fingerprint_cache_ttl_days`).

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS query_canonical_fp TEXT,
    ADD COLUMN IF NOT EXISTS query_footprint JSONB,
    ADD COLUMN IF NOT EXISTS approval_shadow JSONB;

COMMENT ON COLUMN requests.query_canonical_fp IS
$$Canonical-form fingerprint (ast_safety.canonical_fingerprint) of a RO
query, with the `|schema=` suffix when a default schema is set. Layer 1 of
the approval cache.$$;

COMMENT ON COLUMN requests.query_footprint IS
$$Column footprint (ast_safety.footprint) of a RO query:
{"v":1,"complete":bool,"tables":[...],"cols":[[table,column,use],...]}.
Layer 2 of the approval cache.$$;

COMMENT ON COLUMN requests.approval_shadow IS
$$Set when the footprint layer runs in shadow mode and would have approved
this request: {"layer":"footprint","matched":[request ids]}. The request
still went to an admin.$$;

CREATE INDEX IF NOT EXISTS idx_requests_canonical_cache
    ON requests (requester_slack_id, target_server_id, database_name,
                 query_canonical_fp, completed_at)
 WHERE status = 'completed' AND query_canonical_fp IS NOT NULL;

INSERT INTO bot_config (key, value, description) VALUES
    ('approval_cache_canonical', 'on',
     'Approval cache layer 1: a repeat RO query that differs only in form (ORDER BY, SELECT-list order, aliases, AND/OR order, inner-join order, IN-list length) auto-approves like an exact fingerprint match.'),
    ('approval_cache_footprint', 'shadow',
     'Approval cache layer 2: off, shadow or on. A RO query whose every column earlier completed requests already read (same user/target/db/schema, within the TTL) skips review. shadow only records the would-be match in requests.approval_shadow.')
ON CONFLICT (key) DO NOTHING;
