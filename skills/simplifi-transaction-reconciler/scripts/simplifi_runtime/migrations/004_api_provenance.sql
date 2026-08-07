-- Preserve read-side API fields that are absent from CSV exports.
-- Existing installations receive the same columns as fresh installations.

ALTER TABLE transaction_version ADD COLUMN transacted_on TEXT;
ALTER TABLE transaction_version ADD COLUMN account_id TEXT;
ALTER TABLE transaction_version ADD COLUMN inferred_category TEXT;
ALTER TABLE transaction_version ADD COLUMN excluded_from_f2s INTEGER NOT NULL DEFAULT 0;
ALTER TABLE transaction_version ADD COLUMN is_split INTEGER NOT NULL DEFAULT 0;
ALTER TABLE transaction_version ADD COLUMN is_reviewed INTEGER NOT NULL DEFAULT 0;
