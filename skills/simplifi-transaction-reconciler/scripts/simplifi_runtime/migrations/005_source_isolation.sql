-- Keep current transaction versions separate for each source.
-- CSV and API identifiers are not interchangeable, and switching sources
-- should not combine both representations in one analysis.

ALTER TABLE transaction_version ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown';

CREATE INDEX IF NOT EXISTS idx_txn_source_current
    ON transaction_version (source, is_current, transaction_id);
