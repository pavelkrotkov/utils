-- Key incremental cursors by the identity they were read against.
--
-- Before this, `runs.cursor_after` was looked up by source name alone, so a
-- second dataset, a token for another profile, or a changed `--since` window
-- inherited a high-water mark earned against different data. The inheriting run
-- then requests only what changed after that mark and never sees the rest. It
-- reports success.
--
-- Existing rows keep NULL, which is a scope in its own right and matches no
-- resolved scope. The first run after this migration therefore finds no cursor
-- and re-requests its full window once. That is deliberate: the old values
-- cannot be attributed to a scope after the fact, and re-reading an overlap is
-- idempotent while adopting a cursor from unknown provenance is not.
ALTER TABLE runs ADD COLUMN cursor_scope TEXT;

CREATE INDEX IF NOT EXISTS idx_runs_cursor_scope
    ON runs (source, cursor_scope, outcome, id);
