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

-- Eligibility has to be RECOMPUTED here, not merely annotated.
--
-- The old rule required `account_name`, so a row whose name was genuinely empty
-- was stored with `review_eligible = 0` and `missing_required_field`. The new
-- rule makes an unnamed account a diagnostic rather than a disqualification.
-- Prepending the new code and leaving the old verdict in place would keep those
-- rows out of every `analyze` until something happened to re-ingest them — a
-- current row, silently omitted, under a policy that says it is reviewable.
--
-- Only the account-name half of the verdict is revisited. A row missing a
-- transaction ID, a posted date, or an amount is still missing a required
-- field, and a row the user excluded from reports is still excluded; neither is
-- anything this migration learned something new about.
UPDATE transaction_version
SET eligibility_reason_codes = CASE
        WHEN eligibility_reason_codes = '' THEN 'account_name_unknown'
        ELSE 'account_name_unknown,' || eligibility_reason_codes
    END
WHERE account_name_known = 0
  AND eligibility_reason_codes NOT LIKE '%account_name_unknown%';

-- Drop `missing_required_field` where the account name was the only thing
-- missing, and restore the row to review.
UPDATE transaction_version
SET
    eligibility_reason_codes =
        replace(
            replace(eligibility_reason_codes, 'missing_required_field,', ''),
            'missing_required_field',
            ''
        ),
    review_eligible = CASE WHEN exclusion_flag = 1 THEN 0 ELSE 1 END
WHERE account_name_known = 0
  AND eligibility_reason_codes LIKE '%missing_required_field%'
  AND transaction_id IS NOT NULL AND trim(transaction_id) <> ''
  AND posted_on IS NOT NULL AND trim(posted_on) <> ''
  AND amount_minor_units IS NOT NULL;

-- `replace` can leave a trailing separator or an empty slot behind. Tidy them,
-- and make sure a row that is now eligible says so, since `eligible` is a code
-- the packet and the report both read.
UPDATE transaction_version
SET eligibility_reason_codes = trim(
        replace(replace(eligibility_reason_codes, ',,', ','), ' ', ''), ','
    )
WHERE account_name_known = 0;

UPDATE transaction_version
SET eligibility_reason_codes = CASE
        WHEN eligibility_reason_codes = '' THEN 'eligible'
        ELSE eligibility_reason_codes || ',eligible'
    END
WHERE account_name_known = 0
  AND review_eligible = 1
  AND eligibility_reason_codes NOT LIKE '%eligible%';
