-- Preserve why a transaction stopped being current, and who decided so.
--
-- Retirement was a side effect: `is_current` went to 0 and that was the whole
-- record. The row survived, but the *event* did not — nothing said which run
-- retired it, when, or on what grounds. And the grounds differ in a way that
-- matters:
--
--   provider_tombstone     the provider said this transaction was deleted
--   absent_from_snapshot   a complete scan came back without it, so we inferred
--                          it was gone
--
-- The first is testimony; the second is inference from a scan that was assumed
-- complete. When a truncated response is mistaken for a complete one, the
-- second reason produces a wave of retirements that look exactly like the
-- first. Recording which is which is the difference between reconstructing that
-- afternoon and guessing at it.
--
-- Append-only, like `transaction_version`: retirement is an event, and events
-- are not edited. Nothing is backfilled — retirements that predate this table
-- left no evidence to recover, and inventing plausible rows would be worse than
-- an honest gap.
CREATE TABLE IF NOT EXISTS retirement_record (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id   TEXT    NOT NULL,
    source           TEXT    NOT NULL,
    -- The exact version row this retired, not just the transaction. A
    -- transaction can be retired, reappear, and be retired again; without the
    -- version reference those events are indistinguishable.
    prior_version_id INTEGER NOT NULL REFERENCES transaction_version(id),
    run_id           INTEGER NOT NULL REFERENCES runs(id),
    reason           TEXT    NOT NULL,
    retired_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_retirement_transaction
    ON retirement_record (source, transaction_id, id);

CREATE INDEX IF NOT EXISTS idx_retirement_run
    ON retirement_record (run_id, id);

-- Append-only is enforced by the database, not by convention — the same guard
-- `decision_record` carries. A provenance table defended only by the code that
-- happens to write it today is defended by nothing: ad-hoc maintenance, a
-- migration written in a hurry, or a future path with a connection can rewrite
-- the reason, the run, or the timestamp, and the record would still look
-- authoritative afterwards. A retirement that turns out to be wrong is
-- corrected by the transaction reappearing in a later run, which appends its
-- own evidence; it is never corrected by editing history.
CREATE TRIGGER IF NOT EXISTS retirement_record_forbids_update
BEFORE UPDATE ON retirement_record
BEGIN
    SELECT RAISE(ABORT, 'retirement_record is append-only');
END;

CREATE TRIGGER IF NOT EXISTS retirement_record_forbids_delete
BEFORE DELETE ON retirement_record
BEGIN
    SELECT RAISE(ABORT, 'retirement_record is append-only');
END;
