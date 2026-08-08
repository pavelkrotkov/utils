# ADR-006: Store append-only observations with reproducible derivations

- Status: Accepted
- Scope: SQLite storage, synchronization, and audit provenance

## Context

Aggregators can rename, recategorize, re-amount, replace, or remove a
transaction after it first appears. Upserting the latest row loses evidence and
makes it impossible to distinguish data churn from algorithm changes.

## Decision

Use numbered, ordered migrations and an append-only transaction-version table.
Identify API observations with the provider's stable transaction ID; use a
content-addressed synthetic ID only for ID-less exports and treat it as a
fallback that cannot survive edits. Record the source hash and mark the current
version without deleting prior versions.

Record run and fetch state, row counts, source details, outcomes, and analysis
results. Every derived proposal or signal carries enough provenance to
reproduce it: run ID, source hash, algorithm/ruleset version, and when relevant
model ID and prompt version, plus creation time. Preserve raw source evidence
needed to audit normalization and write decisions.

A run's state is explicit — `started`, `succeeded`, `failed`, `aborted` — not
inferred from the presence or absence of an outcome. A single nullable field
cannot distinguish "running now" from "died and is never coming back", so
nothing can act on either, and an unexpected exception leaves a run unfinished
forever. Every exit path, including one nobody anticipated, must move the run
to a terminal state; a failed run records an error class to aggregate by and a
message someone can act on. Interruption is recorded distinctly from failure,
because "someone stopped it" and "something is wrong" call for different
responses. A terminated run counts as interrupted, which means SIGTERM must be
made to raise — its default action ends the process without unwinding, and a
scheduled run is far more often stopped by a service manager than by a
keyboard. A run whose process was killed outright and could not be unwound
keeps `started`: we never learned what happened, and inventing a conclusion is
worse than admitting that.

Retirement is an event, not a flag. Clearing a current-version marker records
that a transaction is no longer current and nothing about why, which run
decided so, or on what grounds — the row survives and the event does not. Append
a retirement record naming the transaction, the exact prior version, the
retiring run, the reason, the timestamp, and the source. The version reference
matters because a transaction can be retired, reappear, and be retired again;
without it those events are indistinguishable.

Keep the grounds distinct. A provider tombstone is testimony that a transaction
was deleted; an absence from a full scan is our own inference from a response we
believed complete. A truncated response mistaken for a complete one produces a
wave of the second kind that looks exactly like the first, so the reason must be
recorded when it is known rather than reconstructed afterwards. Retirement
history stays queryable independently of current state — a retired transaction
is precisely the one whose story the current view cannot tell — while
current-state queries exclude retired rows by default. Retirement is not
permanent: a provider may resurrect a transaction, so "currently retired" is
history confirmed against the absence of a current version, not history alone.
A judgment proposed against a transaction retired since its review packet was
built is refused; the packet still lists it because a packet describes the
database as it was, and appending an immutable decision about something no
longer there would record the judgment and not its irrelevance.

A terminal state is final. Anything raised after a run commits — reporting into
a closed pipe is the everyday case — must not rewrite it, because the rollback
that accompanies a failure cannot take back committed rows, and the result
would be current transaction rows beside a run claiming it failed. Read-only
commands must be able to migrate a database too: a schema change that only
`ingest` can apply turns the first post-upgrade report into an error about our
own schema.

Only a succeeded run is analysis input. Analysis never falls back to selecting
rows by source alone, which would report on whatever a failed or half-finished
run happened to leave behind, and when there is no usable run it says which of
those situations applies rather than reporting an empty result. Recording a
failure must never mask it: the database is one of the things that may be
broken on that path, so a bookkeeping error is reported alongside the original
cause, never in place of it.

For API sources, prefer the provider's incremental modification cursor/as-of
value over repeatedly refetching a full window. Persist the cursor with the
source state, advance it only after a successful fetch, and retain a bounded
full-scan/reconciliation path for recovery. A failed or partial fetch must not
advance the cursor or be recorded as a complete run.

The cursor comes from the response's own as-of marker, never from the records
it returned. A maximum over the returned rows is not a coverage claim: it can
exceed the point the provider had actually published to, and advancing to it
skips the gap forever. When the provider supplies no usable marker, ingest the
rows and leave the cursor where it is — re-reading an overlap is cheap and
idempotent, while a skipped window is silent and permanent. Treat the cursor as
a monotonic watermark: refuse a marker older than the one the run already held,
so a stale replica or a clock rollback cannot rewind it and strand the sync in a
window it re-reads forever. Record the requested cursor and the accepted marker
on the run so an unexpected window can be diagnosed after the fact.

A cursor is meaningless without the identity it was read against, so key it by
that identity: who is asking (profile, authentication subject), what they are
asking about (dataset), and how the question is bounded (explicit query scope).
Changing any component selects a separate history. Keyed by source name alone,
a second dataset or a widened query bound inherits a mark earned against
different data and never fetches what precedes it — the failure is silent and
the run reports success. Store identity components as digests: they are only
ever compared, never read back. Cursors recorded before scoping cannot be
attributed after the fact, so they are retained but never adopted; one wider
re-read is the correct price, and it is reported rather than left to surprise.

## Consequences

Re-ingestion is idempotent for unchanged observations, changes become visible,
and reports can explain which data and code produced them. Incremental runs
reduce load without sacrificing a deliberate recovery path.

## Non-scope

Retention duration, personal transaction data, databases, logs, and undo files
are deployment artifacts, not skill resources.
