-- Append-only decision records for validated agent judgment.
--
-- A record is the terminal artifact of `review-packet.json -> proposals.json`.
-- It records what was decided, about which transaction version of which run,
-- and by which validator. It never authorizes a provider write: `action` is
-- constrained by the validator to read-only follow-ups.

CREATE TABLE IF NOT EXISTS decision_record (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id       TEXT NOT NULL UNIQUE,
    run_id            INTEGER NOT NULL REFERENCES runs(id),
    source            TEXT NOT NULL,
    analysis_date     TEXT NOT NULL,
    transaction_id    TEXT NOT NULL,
    proposal_id       TEXT NOT NULL,
    proposal_hash     TEXT NOT NULL,
    dataset_hash      TEXT NOT NULL,
    decision          TEXT NOT NULL,
    action            TEXT NOT NULL,
    category          TEXT,
    rationale         TEXT NOT NULL,
    reviewer_kind     TEXT NOT NULL,
    reviewer_id       TEXT NOT NULL,
    recorded_at       TEXT NOT NULL,
    validator_version TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_decision_run ON decision_record(run_id);
CREATE INDEX IF NOT EXISTS ix_decision_txn ON decision_record(transaction_id);

-- Append-only is enforced by the database, not by convention. A corrected
-- judgment is a new record with its own hash, never an edit to the old one.
CREATE TRIGGER IF NOT EXISTS decision_record_forbids_update
BEFORE UPDATE ON decision_record
BEGIN
    SELECT RAISE(ABORT, 'decision_record is append-only');
END;

CREATE TRIGGER IF NOT EXISTS decision_record_forbids_delete
BEFORE DELETE ON decision_record
BEGIN
    SELECT RAISE(ABORT, 'decision_record is append-only');
END;
