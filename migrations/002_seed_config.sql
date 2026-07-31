-- Default runtime configuration. Change values via SQL UPDATE; bot reads on each request.

INSERT INTO bot_config (key, value, description) VALUES
    ('max_rows',          '1000', 'Maximum rows returned in CSV result'),
    ('csv_size_mb',       '10',   'Maximum CSV file size in MB'),
    ('query_timeout_sec', '300',  'Per-query statement_timeout in seconds'),
    ('require_justification', 'false', 'Require requester to fill justification field'),
    ('min_query_length',  '6',    'Reject queries shorter than this many chars')
ON CONFLICT (key) DO NOTHING;
