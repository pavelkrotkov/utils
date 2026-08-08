---
name: simplifi-transaction-reconciler
description: Reconcile and review Quicken Simplifi transactions from CSV exports or the private API while preserving accounting semantics, merchant normalization, recurring-charge checks, and an auditable read-only workflow. Use when analyzing Simplifi transaction data, reviewing category findings, checking subscriptions or card credits, or producing proposals for human review.
---

# Simplifi Transaction Reconciler

## Operating contract

Reconcile transactions into an auditable report and explicit proposals. Preserve
raw evidence, source capability, accounting meaning, provenance, and uncertainty.
This skill is strictly read-only: proposals are terminal artifacts for review,
not instructions or authorization to change a live account. Never turn a
forecast into a fact, a display name into identity evidence, or a proposal into
a live account change.

Keep deployment-specific categories, merchant mappings, rules, credentials,
databases, logs, and undo files outside this skill. Load only the reference
material needed for the current decision:

- For source choice, API fields, pagination, or CSV limitations, read
  [ADR-001](references/adr/001-source-strategy.md) and the concise
  [Simplifi API reference](references/simplifi-api.md).
- For exact endpoint, header, schema, pagination, health, or incremental read
  details, load the [Simplifi API reference](references/simplifi-api.md). Its
  observed write and refresh material is reference-only and does not expand
  this skill's capabilities.
- For payee identity or rule matching, read
  [ADR-002](references/adr/002-merchant-identity-and-normalization.md).
- For spending, transfers, refunds, credits, or projections, read
  [ADR-003](references/adr/003-accounting-semantics-and-projections.md).
- For deterministic review, memory, or model residue, read
  [ADR-004](references/adr/004-deterministic-first-escalation.md).
- For the non-executable future mutation design, read
  [ADR-005](references/adr/005-safe-mutation-and-approval.md). Do not execute
  or improvise that design in the current skill.
- For storage, incremental sync, or reproducibility, read
  [ADR-006](references/adr/006-provenance-and-incremental-storage.md).
- For the deterministic agent boundary and its safe field allowlist, read the
  [review-packet contract](references/review-packet.md).
- For returning judgment as validated proposals and append-only decision
  records, read the
  [decision-record contract](references/decision-records.md).
- For tokens, unattended execution, or deployment, read
  [ADR-007](references/adr/007-authentication-and-deployment.md).
- For the future Hermes browser-session authentication architecture, load the
  [Hermes auth reference](references/hermes-auth.md). The refresher is not part
  of this skill; accept an externally supplied token and stop when it is stale.
  Keep secrets, account configuration, and host-specific instructions out of
  this skill.
- Read [judgment examples](references/examples/judgment-examples.md) when a
  review resembles a prior case or a genuinely new human decision is being
  recorded. Do not copy detailed reference content into this file.

## Capability boundary

The packaged runtime supports only these operations:

- ingest CSV exports and read transaction data from the private API;
- normalize, store provenance, analyze, and render reports locally;
- inspect API read schemas and connection health;
- optionally ask a model to classify unresolved rows and write proposal files;
- validate structured agent proposals and append local decision records.

The following are explicitly unavailable: login or access-token refresh, bank or
institution refresh, notifications, account writes, transaction/category/rule
writes, proposal approval or application, and undo/rollback. If any of these is
requested, report that it is unavailable and stop. Do not invent an endpoint or
call an observed endpoint from the reference material merely because it exists.
Recording a decision is not approving one: a decision record documents a
judgment for human review and never authorizes a provider write.

## Packaged read/analyze runtime

The reusable implementation is under `scripts/`. Run the entrypoint from this
directory or by absolute path; it has no dependency on any other repository
files required at runtime. The curated judgment examples are bundled alongside
the runtime, with the skill reference used when available:

```bash
uv run ./scripts/simplifi_transaction_reconciler.py ingest \
  --source csv /path/to/Simplifi-Transactions.csv --db /path/to/review.sqlite
uv run ./scripts/simplifi_transaction_reconciler.py ingest \
  --source api --full-rescan --db /path/to/review.sqlite
uv run ./scripts/simplifi_transaction_reconciler.py analyze \
  --db /path/to/review.sqlite --out /path/to/review.html
uv run ./scripts/simplifi_transaction_reconciler.py subs --db /path/to/review.sqlite
uv run ./scripts/simplifi_transaction_reconciler.py classify \
  --db /path/to/review.sqlite --dry-run
uv run ./scripts/simplifi_transaction_reconciler.py decide \
  --db /path/to/review.sqlite --packet /path/to/review-packet.json \
  --proposals /path/to/proposals.json --out /path/to/decisions.json
```

`analyze` also emits `review-packet.json` beside the HTML report by default.
Use `--packet-out` to choose another path. The packet is versioned, validated,
deterministically ordered, read-only, and contains only normalized review
evidence plus provenance; it excludes raw descriptors, account IDs, source
paths, and credentials. It is the artifact passed to agent judgment. The
runtime loads only the explicitly promoted, sanitized cases from
`references/examples/judgment-examples.md` (or its bundled runtime copy), selects relevant cases
deterministically, and includes them in the packet. The classifier prompt uses
the same curated context; categorized transaction history is never promoted
automatically into reusable examples.

`decide` closes the boundary. It validates a structured `proposals.json`
against one review packet and appends immutable decision records to the store,
writing the validated result to a separate `--out` file. Proposals are rejected
whole — nothing is recorded — when they name an unknown transaction, carry a
malformed decision, request an unsupported or mutating action, propose a
category the dataset does not use, omit a rationale, or reference a run that a
later ingest has superseded. Every rejection reports its JSON path and code.

Use `--source api` for read-only API ingestion, or `probe`/`schema` for
read-only diagnostics. Omit `--modified-after` for normal API ingestion to use
the last successful cursor; use `--full-rescan` for recovery or after changing
derivation rules. That cursor is scoped to the profile, dataset, token subject,
and `--since` bound it was earned under, so pointing the same database at
another dataset or widening `--since` starts a separate history instead of
inheriting a mark that does not describe it. Both `ingest` and `probe` report
the scope in use. The packaged CLI emits reports, diagnostics, prompts, and
proposal files only; it never writes provider state, refreshes an institution,
sends notifications, or undoes a change. CSV runs report their limitation
because CSV exports do not expose settlement or projection state.

## Repeatable workflow

1. **Declare the run.** Record the analysis date, requested scope, mode
   (`scheduled` or `read-only`), source, and ruleset/profile versions. Treat
   missing capabilities as limitations to report, not as permission to guess.
2. **Choose and validate the source.** Use CSV for portable offline analysis or
   cross-checks. Use the private API when stable IDs, raw descriptors,
   account/category IDs, pending state, or scheduled markers are required.
   Follow API cursors exactly, detect duplicates, bound paging, and fail
   clearly on auth/schema errors or incomplete fetches.
3. **Ingest with provenance.** Store observations append-only, keyed by the
   provider transaction ID when available; use a content hash only as the
   fallback for ID-less exports. Record run/fetch state, counts, source hashes,
   current versions, and enough raw evidence to reproduce decisions. Every run
   reaches an explicit terminal state — `succeeded`, `failed`, or `aborted` —
   even when the failure is one nobody anticipated, and a failure records its
   error class and an actionable message. Only a succeeded run is analysis
   input; an unfinished or failed run is never reported on. Advance
   an incremental cursor only after a complete successful fetch, and only to
   the value the response itself declares it is current as of — never to a
   maximum derived from the returned rows, which can jump past records the
   provider had not yet published.
4. **Normalize identity.** Preserve `raw`, deterministic `normalized`, stable
   lowercase `canonical`, and human-facing `display` values. Record rules that
   fired. Group merchants, history, recurring series, and collisions by
   `canonical`, never by `display`; use raw statement names only as read-side
   evidence.
5. **Resolve semantics before statistics.** Classify each row using provider
   fields first and transparent local evidence second. Keep transfers, card
   payments, investments, and balance adjustments out of spending statistics
   and merchant memory. Keep refunds, income, statement credits, and other
   signed meanings distinct; a positive amount alone is not income.
6. **Analyze settled facts.** For spending and recurring-charge totals, include
   only rows with `posted_on <= analysis_date` and confirmed cleared/real state.
   Exclude scheduled/projection rows. Pending is neither settled nor proof of a
   future charge. If the source cannot distinguish these states, report the
   limitation and do not infer.
7. **Apply deterministic review first.** Check category drift, recurring
   anomalies, high-value or processor-fronted charges, rule collisions, and
   uncategorized rows. Use merchant memory only with sufficient observations
   and purity; otherwise retain ambiguity. Keep every signal explainable.
8. **Escalate the residue.** Optional model inference may see only eligible
   unresolved rows and minimum necessary context. Reject unknown category IDs;
   an outage yields a degraded report with unresolved residue. Models emit
   proposal files for review only; they never write provider state.
9. **Report and preserve the decision trail.** Include matched scope, excluded
   semantics, limitations, proposals, unresolved items, confidence/evidence,
   and provenance (`run_id`, source hash, ruleset/algorithm version, and model
   or prompt version when applicable).
10. **Record judgment through the validated boundary.** Return agent decisions
    as `proposals.json` against the packet that was reviewed, and record them
    with `decide`. Correct a recorded decision by appending a new one, never by
    editing history. A rejected proposal is a signal to fix the proposal or
    re-run the analysis, not to bypass validation.

## Human escalation format

Use one record per transaction or coherent merchant group:

```text
ESCALATE
scope: <transaction IDs or canonical merchant and date range>
reason: <specific ambiguity, risk, or missing discriminator>
evidence: <raw descriptor, display value, dates, amounts, state, source>
deterministic finding: <what the rules establish and exclude>
uncertainty: <what remains unknown and why>
proposal: <suggested category, follow-up question, review, or no action>
impact: <affected count/amount; no provider state changes are made>
decision needed: <the smallest human choice required>
provenance: <run ID, source hash, ruleset version>
```

Do not present a confidence score without its evidence. High-value,
processor-fronted, rebranded, or benefit-related cases need confirmation when
the consequence is material. Track card benefits and statement credits against
their allowance or reimbursement semantics; do not inflate income or spending.

## Scheduled and unattended boundary

Scheduled execution is explicitly read-only: validate an externally supplied
token, ingest, analyze, store provenance, and report. It may not refresh a
token, refresh a bank, notify, approve proposals, edit categories, create
rules, or undo changes. Keep authentication separate from API access; do not
replay a client secret or store a password, and stop for interactive
reauthentication when the session is stale, revoked, or changed. Protect
secrets/session state, log keys only, and classify the stored outcome as
`success`, `degraded`, or `hard failure`.

## Future mutation design (non-executable)

ADR-005 records a possible approval, validation, audit, and rollback boundary
for a future mutation issue. It is not an implementation plan to execute during
this skill invocation. The current runtime has no mutation command and this
skill must never send provider write, refresh, notification, or undo requests.
Treat proposal files as terminal outputs until a separately implemented,
explicitly authorized capability exists.

## Recording a genuinely new decision

Add an example only after a human has made a decision that is not already
covered by the existing examples or deterministic rules. Append a portable,
sanitized case to
`references/examples/judgment-examples.md` using exactly these headings:

```markdown
## N. Short decision name

**Situation**
What triggered review.

**Evidence**
Relevant source fields and constraints, without personal transaction data.

**Proposal or escalation**
What was presented and why deterministic analysis stopped there.

**Human decision**
The decision actually made and its scope.

**Reusable lesson**
The general rule future reconciliations should apply or test.
```

Increment `N`, remove identifying values, preserve the reasoning rather than
the account's private rule set, and do not record a hypothetical decision.
