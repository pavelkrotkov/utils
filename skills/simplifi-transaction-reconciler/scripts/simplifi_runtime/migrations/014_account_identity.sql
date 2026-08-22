-- Say out loud whether the account has a name, instead of inferring it.
--
-- The API adapter used to write `accountId` into `account_name` when an
-- account had no name. Downstream that was indistinguishable from a real name:
-- the report rendered it, the packet published it, and the recurring-series
-- grouping keyed on it. The value was a provider identifier the whole way, and
-- nothing in the row said so.
--
-- The adapter now records an unnamed account as unnamed, and this column
-- carries the fact explicitly so a reader never has to reconstruct it from a
-- string comparison against a column that may itself be absent.
ALTER TABLE transaction_version ADD COLUMN account_name_known INTEGER NOT NULL DEFAULT 1;

-- Existing rows get the same answer the adapter would give them now. A stored
-- name identical to the stored account ID is the old fallback, not a name;
-- an empty name was never one either. Everything else is left alone, because
-- it was and remains a genuine account name.
UPDATE transaction_version
SET account_name_known = 0
WHERE account_name IS NULL
   OR trim(account_name) = ''
   OR (account_id IS NOT NULL AND trim(account_id) <> '' AND account_name = account_id);

-- The old fallback value is a provider identifier sitting in a display column,
-- and it stays reachable through `account_id` where the egress allowlist and
-- the packet contract already refuse it by name. Clear it here so no reader
-- that missed the flag can render it.
UPDATE transaction_version
SET account_name = ''
WHERE account_name_known = 0;

-- Rows whose eligibility was decided when a name was required, and which the
-- ID fallback silently rescued, now carry the diagnostic instead. Row-level
-- codes are re-derived on the next ingest; this keeps stored rows readable in
-- the meantime rather than leaving a code that no longer exists in the code.
UPDATE transaction_version
SET eligibility_reason_codes = CASE
        WHEN eligibility_reason_codes = '' THEN 'account_name_unknown'
        ELSE 'account_name_unknown,' || eligibility_reason_codes
    END
WHERE account_name_known = 0
  AND eligibility_reason_codes NOT LIKE '%account_name_unknown%';
