-- Attribute materialized transaction state to the scope that read it.
--
-- Migration 010 scoped the synchronization *cursor* by profile, dataset, auth
-- subject and `--since`. It did not scope what the cursor produced: current
-- rows were isolated by `source` alone, so every API scope shared one set. Two
-- datasets in one database interfered in two directions. A complete rescan
-- under scope B retired scope A's rows, because they were absent from B's
-- observed-ID set and nothing distinguished them. Incremental runs under either
-- scope upserted into the shared set, so `analyze` reported on a mixture and
-- said nothing about it.
--
-- Migration 011's `complete_snapshot` guard made that collision expensive
-- rather than lossy: a scope whose rows had been replaced by another refused
-- its cursor and re-read its full window. With state scoped, the collision does
-- not happen, and the guard is no longer consulted. The column stays — it is
-- true run provenance, `status` reports it, and dropping a recorded fact to
-- tidy up a schema is not a trade this project makes.
--
-- Existing rows get NULL, which is the legacy scope: a real scope, distinct
-- from every resolved one, holding exactly the rows written before scoping
-- existed. It is not a null-as-wildcard. The runtime adopts those rows into the
-- first scope that ingests afterwards, but only when the run history shows a
-- single scope ever succeeded for that source; where several did, the legacy
-- rows are a mixture nobody can attribute, and the runtime says so instead of
-- guessing.
ALTER TABLE transaction_version ADD COLUMN cursor_scope TEXT;

-- Retirement rows carry the scope they were retired under for the same reason:
-- `retired_transaction_ids` asks whether a transaction is gone, and a
-- transaction retired in one dataset is not gone from another. Legacy rows keep
-- NULL here permanently — the table is append-only, enforced by trigger, so
-- there is no honest way to backfill an attribution that was never observed.
ALTER TABLE retirement_record ADD COLUMN cursor_scope TEXT;

-- Current-row lookups and snapshot retirement now filter on all three.
CREATE INDEX IF NOT EXISTS idx_txn_scope_current
    ON transaction_version (source, cursor_scope, is_current);

CREATE INDEX IF NOT EXISTS idx_retirement_scope
    ON retirement_record (source, cursor_scope, transaction_id);
