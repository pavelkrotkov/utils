---
name: simplifi-transaction-reconciler
description: Reconcile and review Quicken Simplifi transactions from CSV exports or the private API while preserving accounting semantics, merchant normalization, recurring-charge checks, and guarded account writes. Use when analyzing Simplifi transaction data, reviewing category or rule opportunities, checking subscriptions or card credits, or safely applying and undoing account changes.
---

# Simplifi Transaction Reconciler

## Operating contract

Reconcile transactions into an auditable report and explicit proposals. Preserve
raw evidence, source capability, accounting meaning, provenance, and uncertainty.
Never turn a forecast into a fact, a display name into identity evidence, or a
proposal into a live account change without the approval boundary below.

Keep deployment-specific categories, merchant mappings, rules, credentials,
databases, logs, and undo files outside this skill. Load only the reference
material needed for the current decision:

- For source choice, API fields, pagination, or CSV limitations, read
  [ADR-001](references/adr/001-source-strategy.md) and the concise
  [Simplifi API reference](references/simplifi-api.md).
- For exact endpoint, header, schema, pagination, write, health, or incremental
  sync details, load the [Simplifi API reference](references/simplifi-api.md).
- For payee identity or rule matching, read
  [ADR-002](references/adr/002-merchant-identity-and-normalization.md).
- For spending, transfers, refunds, credits, or projections, read
  [ADR-003](references/adr/003-accounting-semantics-and-projections.md).
- For deterministic review, memory, or model residue, read
  [ADR-004](references/adr/004-deterministic-first-escalation.md).
- For any category/rule write or undo, read
  [ADR-005](references/adr/005-safe-mutation-and-approval.md).
- For storage, incremental sync, or reproducibility, read
  [ADR-006](references/adr/006-provenance-and-incremental-storage.md).
- For tokens, unattended execution, or deployment, read
  [ADR-007](references/adr/007-authentication-and-deployment.md).
- For the current browser-session refresh architecture or Hermes build and
  failure checklist, load the [Hermes auth reference](references/hermes-auth.md).
  Keep secrets, account configuration, and host-specific instructions out of
  this skill.
- Read [judgment examples](references/examples/judgment-examples.md) when a
  review resembles a prior case or a genuinely new human decision is being
  recorded. Do not copy detailed reference content into this file.

## Packaged read/analyze runtime

The reusable implementation is under `scripts/`. Run the entrypoint from this
directory or by absolute path; it has no dependency on any other repository
files:

```bash
uv run ./scripts/simplifi_transaction_reconciler.py ingest \
  --source csv /path/to/Simplifi-Transactions.csv --db /path/to/review.sqlite
uv run ./scripts/simplifi_transaction_reconciler.py analyze \
  --db /path/to/review.sqlite --out /path/to/review.html
uv run ./scripts/simplifi_transaction_reconciler.py subs --db /path/to/review.sqlite
uv run ./scripts/simplifi_transaction_reconciler.py classify \
  --db /path/to/review.sqlite --dry-run
```

Use `--source api` for read-only API ingestion, or `probe`/`schema` for
read-only diagnostics. The packaged CLI emits reports and proposal files only;
it contains no account rule plan, account assignments, benefit allowances, or
category/rule apply or undo workflow. CSV runs report their limitation because
CSV exports do not expose settlement or projection state.

## Repeatable workflow

1. **Declare the run.** Record the analysis date, requested scope, mode
   (`scheduled`, `read-only`, or explicitly approved `mutate`), source, and
   ruleset/profile versions. Treat missing capabilities as limitations to
   report, not as permission to guess.
2. **Choose and validate the source.** Use CSV for portable offline analysis or
   cross-checks. Use the private API when stable IDs, raw descriptors,
   account/category IDs, pending state, scheduled markers, splits, or writes
   are required. Follow API cursors exactly, detect duplicates, bound paging,
   and fail clearly on auth/schema errors or incomplete fetches.
3. **Ingest with provenance.** Store observations append-only, keyed by the
   provider transaction ID when available; use a content hash only as the
   fallback for ID-less exports. Record run/fetch state, counts, source hashes,
   current versions, and enough raw evidence to reproduce decisions. Advance
   an incremental cursor only after a complete successful fetch.
4. **Normalize identity.** Preserve `raw`, deterministic `normalized`, stable
   lowercase `canonical`, and human-facing `display` values. Record rules that
   fired. Group merchants, history, recurring series, and collisions by
   `canonical`, never by `display`; use raw statement names for provider rules.
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
   proposals for review, never writes.
9. **Report and preserve the decision trail.** Include matched scope, excluded
   semantics, limitations, proposals, unresolved items, confidence/evidence,
   and provenance (`run_id`, source hash, ruleset/algorithm version, and model
   or prompt version when applicable).

## Human escalation format

Use one record per transaction or coherent merchant group:

```text
ESCALATE
scope: <transaction IDs or canonical merchant and date range>
reason: <specific ambiguity, risk, or missing discriminator>
evidence: <raw descriptor, display value, dates, amounts, state, source>
deterministic finding: <what the rules establish and exclude>
uncertainty: <what remains unknown and why>
proposal: <category, rule, review, or no action>
impact: <affected count/amount and whether history or future rows change>
decision needed: <the smallest human choice required>
provenance: <run ID, source hash, ruleset version>
```

Do not present a confidence score without its evidence. High-value,
processor-fronted, rebranded, or benefit-related cases need confirmation when
the consequence is material. Track card benefits and statement credits against
their allowance or reimbursement semantics; do not inflate income or spending.

## Scheduled and unattended boundary

Scheduled execution is explicitly read-only: validate/refresh a token, ingest,
analyze, store provenance, report, and notify. It may not approve proposals,
edit categories, create rules, or undo changes. Keep authentication separate
from API access; use a fresh bearer token, do not replay a client secret or
store a password, and stop for interactive reauthentication when the session
is stale, revoked, or changed. Protect secrets/session state, log keys only,
and classify the stored outcome as `success`, `degraded`, or `hard failure`.

## Guarded mutation protocol

Mutation is dry-run by default and requires an explicit interactive approval or
commit. A proposal file contains decisions only; load authoritative IDs,
categories, hashes, and original values from the local store.

Before each write:

1. Re-fetch the live record and require an undecided proposal whose source hash
   and original category still match.
2. Resolve categories/rules against the live provider. Reject unsupported
   transfers, splits, and already-changed records.
3. For full-document APIs, deep-copy the live document, change only the target
   field, and preserve the exact pre-write document before sending.
4. For rules, use narrow terms, test expected matches and collisions across
   the complete dataset, skip equivalent existing rules, and remain idempotent.
5. Rate-limit writes, poll asynchronous jobs, and record before/after payloads,
   responses, approval, and resolved job IDs.

Undo restores the saved prior document only when the provider permits it; never
   claim rollback is guaranteed. Re-fetch and verify after the operation.

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
