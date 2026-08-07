-- Preserve the distinction between broad review eligibility and settled-only
-- analysis.  Missing optional source fields remain visible with diagnostics.

ALTER TABLE transaction_version ADD COLUMN review_eligible INTEGER NOT NULL DEFAULT 1;
ALTER TABLE transaction_version ADD COLUMN eligibility_reason_codes TEXT NOT NULL DEFAULT '';

-- Defaults preserve compatibility for the ALTER TABLE itself, but existing
-- rows must receive the same diagnostics as newly ingested rows immediately.
UPDATE transaction_version
SET
    review_eligible = CASE WHEN exclusion_flag = 1 THEN 0 ELSE 1 END,
    eligibility_reason_codes = CASE
        WHEN exclusion_flag = 1 THEN 'excluded_from_reports'
        WHEN exclusion_flag = 2 AND (txn_state IS NULL OR trim(txn_state) = '')
            THEN 'report_exclusion_unknown,missing_optional_field,eligible'
        WHEN exclusion_flag = 2 AND upper(trim(txn_state)) <> 'CLEARED'
            THEN 'report_exclusion_unknown,unsupported_state,eligible'
        WHEN exclusion_flag = 2 THEN 'report_exclusion_unknown,eligible'
        WHEN txn_state IS NULL OR trim(txn_state) = ''
            THEN 'missing_optional_field,eligible'
        WHEN upper(trim(txn_state)) <> 'CLEARED'
            THEN 'unsupported_state,eligible'
        ELSE 'eligible'
    END;
