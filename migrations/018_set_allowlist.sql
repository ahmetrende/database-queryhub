-- Allowed SET parameters for the optional `SET LOCAL <param> = ...`
-- prelude that may precede a /sql request. The bot rewrites plain `SET`
-- to `SET LOCAL` automatically (transaction-scoped). The list is a
-- comma-separated whitelist of safe tuning parameters; any SET on a
-- parameter not in this list is rejected at safety-analysis time with
-- a specific error naming the offending parameter.
--
-- Edit live to add/remove parameters; bot reads on every submission.
--
-- Explicitly NOT included (do not add unless you know what you are
-- doing): search_path, role, session_authorization, current_user,
-- authorization, client_min_messages, log_*, row_security.

INSERT INTO bot_config (key, value, description) VALUES
    ('set_allowed_params',
     'work_mem,statement_timeout,lock_timeout,idle_in_transaction_session_timeout,'
     'enable_seqscan,enable_indexscan,enable_bitmapscan,enable_hashjoin,'
     'enable_mergejoin,enable_nestloop,enable_indexonlyscan,'
     'random_page_cost,seq_page_cost,cpu_tuple_cost,cpu_index_tuple_cost,'
     'cpu_operator_cost,effective_cache_size,default_statistics_target,'
     'geqo,geqo_threshold,from_collapse_limit,join_collapse_limit,jit',
     'Comma-separated allowlist of parameter names usable in a `SET LOCAL <param> = <value>` prelude before the main query. Bot auto-rewrites plain `SET` to `SET LOCAL`. SET on any parameter outside this list is rejected with a specific error.')
ON CONFLICT (key) DO NOTHING;
