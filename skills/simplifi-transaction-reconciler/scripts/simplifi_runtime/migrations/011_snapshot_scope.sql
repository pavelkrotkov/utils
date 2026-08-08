-- Record which runs replaced the materialized snapshot, so a cursor is never
-- reused across a snapshot that belongs to a different scope.
--
-- Cursor scoping alone is not enough. `transaction_version` is still isolated
-- by source, so a complete rescan under scope B retires scope A's current rows.
-- If A then runs incrementally against its own — still perfectly valid —
-- cursor, it fetches only post-cursor deltas and never restores the history B
-- retired. A's data stays missing and the run reports success.
--
-- Marking the snapshot's owner lets the runtime notice that the rows on disk
-- were last replaced by someone else and decline to trust its cursor. Scoping
-- the transaction state itself is the fuller fix and is tracked separately;
-- this closes the data-loss path without pretending the two scopes can share a
-- materialized view.
ALTER TABLE runs ADD COLUMN complete_snapshot INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_runs_snapshot_owner
    ON runs (source, complete_snapshot, outcome, id);
