-- Per-request result format ('csv' | 'xlsx'). Bot still gates by
-- wants_result first (FALSE = no file at all, regardless of format).
-- Default 'csv' so existing rows keep the historical behaviour.

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS result_format TEXT NOT NULL DEFAULT 'csv'
    CHECK (result_format IN ('csv', 'xlsx'));

COMMENT ON COLUMN requests.result_format IS
$$Output format the requester picked in the modal: 'csv' (default, single CSV upload) or 'xlsx' (Excel workbook, openpyxl-built). wants_result=FALSE overrides — no file produced regardless of format.$$;
