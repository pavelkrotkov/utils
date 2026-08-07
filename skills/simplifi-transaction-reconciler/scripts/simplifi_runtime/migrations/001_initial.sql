-- Simplifi transaction reconciler initial schema.
-- Append-only transaction versions and run state.
--
-- NOTE ON IDENTITY: the Simplifi CSV export carries no transaction ID, so
-- `transaction_id` here is a synthetic content hash. It is stable for
-- unchanged rows but cannot survive an edit to date/account/payee/amount.
-- The API supplies the authoritative ID; CSV uses the synthetic fallback.

CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    outcome           TEXT,              -- success | degraded | failure
    source            TEXT NOT NULL,     -- csv | api | fixture
    source_detail     TEXT,
    algorithm_version TEXT NOT NULL,
    ruleset_version   TEXT NOT NULL,
    row_count         INTEGER
);

CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transaction_version (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id     TEXT NOT NULL,
    run_id             INTEGER NOT NULL REFERENCES runs(id),
    observed_at        TEXT NOT NULL,
    source_hash        TEXT NOT NULL,
    is_current         INTEGER NOT NULL DEFAULT 1,

    posted_on          TEXT NOT NULL,
    account_name       TEXT NOT NULL,
    amount_minor_units INTEGER NOT NULL,
    currency           TEXT NOT NULL DEFAULT 'USD',
    currency_exponent  INTEGER NOT NULL DEFAULT 2,

    payee_raw          TEXT NOT NULL,
    payee_normalized   TEXT NOT NULL,
    payee_canonical    TEXT NOT NULL,
    payee_display      TEXT NOT NULL,
    norm_rules_applied TEXT NOT NULL DEFAULT '',
    -- Pre-conversion charge details parsed out of the payee string. Purely
    -- informational: the issuer already converted, so amount_minor_units and
    -- currency above are USD. Never derive currency from these.
    original_currency  TEXT,
    original_amount    TEXT,
    is_foreign_charge  INTEGER NOT NULL DEFAULT 0,

    category           TEXT NOT NULL DEFAULT '',
    is_uncategorized   INTEGER NOT NULL DEFAULT 0,
    exclusion_flag     INTEGER NOT NULL DEFAULT 0,
    recurring_flag     INTEGER NOT NULL DEFAULT 0,

    kind               TEXT NOT NULL,
    poisons_statistics INTEGER NOT NULL DEFAULT 0,
    semantics_reasons  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_txnver_txid    ON transaction_version(transaction_id);
CREATE INDEX IF NOT EXISTS ix_txnver_current ON transaction_version(is_current);
CREATE INDEX IF NOT EXISTS ix_txnver_canon   ON transaction_version(payee_canonical);
CREATE INDEX IF NOT EXISTS ix_txnver_posted  ON transaction_version(posted_on);
