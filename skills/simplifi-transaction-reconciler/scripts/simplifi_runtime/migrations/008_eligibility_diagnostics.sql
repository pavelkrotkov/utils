-- Preserve the distinction between broad review eligibility and settled-only
-- analysis.  Missing optional source fields remain visible with diagnostics.

ALTER TABLE transaction_version ADD COLUMN review_eligible INTEGER NOT NULL DEFAULT 1;
ALTER TABLE transaction_version ADD COLUMN eligibility_reason_codes TEXT NOT NULL DEFAULT '';
