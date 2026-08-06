-- Store `state` and `matchState`, which the API returns and the CSV cannot.
--
-- WHY THIS EXISTS. A duplicate detector can see multiple rows for one charge
-- while a transaction moves from pending to cleared. Without settlement state,
-- it cannot distinguish that normal lifecycle from a genuine duplicate.
--
-- I could not tell which, because this table dropped both fields that answer it.
-- Pending/posted pairs are
-- common enough to be the leading alternative explanation for ANY duplicate
-- flag. A duplicate detector that cannot distinguish those two cases is not
-- trustworthy — it will cry wolf on ordinary settlement, and the one time it is
-- right nobody will believe it.
--
-- `matchState` is Simplifi's own view of whether a downloaded transaction has
-- been reconciled against another. Keeping both means the detector can say
-- "three CLEARED charges" (act on it) rather than "three rows" (shrug).
--
-- SQLite ALTER TABLE ADD COLUMN is safe here: append-only table, no rewrite,
-- existing rows get NULL, which reads correctly as "source did not say".

ALTER TABLE transaction_version ADD COLUMN txn_state TEXT;
ALTER TABLE transaction_version ADD COLUMN match_state TEXT;

-- Duplicate scanning always filters on these, so index them together with the
-- date; without it the detector does a full scan per candidate group.
CREATE INDEX IF NOT EXISTS idx_txn_state
    ON transaction_version (is_current, txn_state, posted_on);
