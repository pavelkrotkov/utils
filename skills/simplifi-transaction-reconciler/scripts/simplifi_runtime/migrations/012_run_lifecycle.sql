-- Give runs an explicit lifecycle instead of inferring one from `outcome`.
--
-- `outcome` had three reachable values — 'success', 'failure', and NULL — and
-- NULL carried two incompatible meanings: "running right now" and "died and is
-- never coming back". Nothing could tell them apart, so nothing could act on
-- either. An unexpected exception left a run NULL forever, and a reader had no
-- way to know whether to wait for it or write it off.
--
-- `state` names the four situations that actually occur:
--   started    work is in progress, or the process died without finalizing
--   succeeded  finished, everything committed
--   failed     finished with a recorded error
--   aborted    interrupted (Ctrl-C, SIGTERM) before it could finish
--
-- Backfill maps NULL to 'aborted', not 'started': every run predating this
-- migration belongs to a process that is long gone, so "in progress" would be
-- a lie that keeps a dead run eligible forever.
--
-- `outcome` stays, written in lockstep from `state` by a single mapping so the
-- two cannot drift. It is legacy: nothing reads it, and an operator's existing
-- ad-hoc query keeps working rather than silently returning nothing.
ALTER TABLE runs ADD COLUMN state TEXT NOT NULL DEFAULT 'started';

-- Why a run failed, in terms someone can act on. Split so the class can be
-- aggregated ("how often is this an AuthError?") while the message stays
-- readable.
ALTER TABLE runs ADD COLUMN error_class TEXT;
ALTER TABLE runs ADD COLUMN error_message TEXT;

UPDATE runs SET state = CASE
    WHEN outcome = 'success' THEN 'succeeded'
    WHEN outcome IS NULL THEN 'aborted'
    ELSE 'failed'
END;

CREATE INDEX IF NOT EXISTS idx_runs_state ON runs (source, state, id);
