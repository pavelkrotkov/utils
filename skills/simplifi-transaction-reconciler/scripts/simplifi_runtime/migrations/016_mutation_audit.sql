-- Every attempt to change a live financial account, and what became of it.
--
-- Two tables, because a mutation has two moments and only the first one is
-- guaranteed to happen. The attempt is everything known before the request
-- leaves; the outcome is what came back. An attempt with no outcome row is a
-- write that left and never settled — recorded by construction rather than by
-- a process remembering to write it down on the way out. A single row with a
-- nullable `outcome` could not say that: it would have to be inserted after
-- settlement, and a killed process would leave no trace of the write at all.
--
-- Both are append-only, enforced by trigger, like `decision_record` and
-- `retirement_record`. An audit trail defended only by the code that happens to
-- write it today is defended by nothing. That also means the attempt row must
-- be committed before the request, not alongside it.

-- What we intended, what it was about, and who said yes.
CREATE TABLE IF NOT EXISTS mutation_attempt (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id         TEXT    NOT NULL UNIQUE,
    capability         TEXT    NOT NULL,
    transaction_id     TEXT    NOT NULL,
    -- Which judgment this carries out, and which run that judgment was about.
    decision_id        TEXT,
    run_id             INTEGER REFERENCES runs(id),
    source             TEXT    NOT NULL,
    -- The stored row this was planned against, so a plan built against a
    -- snapshot that has since moved can be told from one that has not.
    source_hash        TEXT    NOT NULL,
    -- Who authorized it, in their own words, and when. Not a boolean: "someone
    -- said yes at some point" is not an authorization anybody can audit.
    authorized_by      TEXT    NOT NULL,
    authorization_note TEXT    NOT NULL,
    authorized_at      TEXT    NOT NULL,
    -- The complete documents as JSON text. `before_document` is what the
    -- provider served immediately before the write; `after_document` is what
    -- was sent. Both in full, not a diff: an undo has to reconstruct a whole
    -- document, and a diff against a document nobody kept is not a document.
    before_document    TEXT    NOT NULL,
    after_document     TEXT    NOT NULL,
    -- What changed, in one line, for a human reading the trail.
    change_summary     TEXT    NOT NULL,
    attempted_at       TEXT    NOT NULL,
    -- The attempt this one reverses, when it is an undo. Written at insert and
    -- pointing backwards, so neither row is ever edited.
    undoes_attempt_id  TEXT    REFERENCES mutation_attempt(attempt_id)
);

-- What the provider did about it.
CREATE TABLE IF NOT EXISTS mutation_outcome (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id     TEXT    NOT NULL UNIQUE REFERENCES mutation_attempt(attempt_id),
    -- 'succeeded' — the provider's job settled successfully.
    -- 'failed'    — the provider rejected it; the account is unchanged.
    -- 'unknown'   — a settled result never arrived, and whether the write
    --               landed cannot be established from here. Distinct from
    --               'failed' on purpose: reporting an uncertain write as a
    --               failure would invite a retry that double-applies it.
    outcome        TEXT    NOT NULL,
    job_id         TEXT,
    job_status     TEXT,
    error_class    TEXT,
    error_message  TEXT,
    settled_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_mutation_transaction
    ON mutation_attempt (transaction_id, id);

CREATE INDEX IF NOT EXISTS ix_mutation_decision
    ON mutation_attempt (decision_id);

CREATE INDEX IF NOT EXISTS ix_mutation_undo
    ON mutation_attempt (undoes_attempt_id);

CREATE TRIGGER IF NOT EXISTS mutation_attempt_forbids_update
BEFORE UPDATE ON mutation_attempt
BEGIN
    SELECT RAISE(ABORT, 'mutation_attempt is append-only');
END;

CREATE TRIGGER IF NOT EXISTS mutation_attempt_forbids_delete
BEFORE DELETE ON mutation_attempt
BEGIN
    SELECT RAISE(ABORT, 'mutation_attempt is append-only');
END;

CREATE TRIGGER IF NOT EXISTS mutation_outcome_forbids_update
BEFORE UPDATE ON mutation_outcome
BEGIN
    SELECT RAISE(ABORT, 'mutation_outcome is append-only');
END;

CREATE TRIGGER IF NOT EXISTS mutation_outcome_forbids_delete
BEFORE DELETE ON mutation_outcome
BEGIN
    SELECT RAISE(ABORT, 'mutation_outcome is append-only');
END;
