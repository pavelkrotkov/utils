-- Record the derivation versions that produced each stored transaction row.
-- Re-ingestion after a rule change must append a new version even when source
-- fields are unchanged.

ALTER TABLE transaction_version ADD COLUMN algorithm_version TEXT NOT NULL DEFAULT '0.1.0';
ALTER TABLE transaction_version ADD COLUMN ruleset_version TEXT NOT NULL DEFAULT '0.1.0';
