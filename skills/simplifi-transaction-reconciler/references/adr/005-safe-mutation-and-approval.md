# ADR-005: Make mutation explicit, validated, and recoverable

> **Implemented for one capability.** `transaction_category` follows this
> protocol; the [mutation register](../mutations.md) records what is available,
> what is refused, and why. Everything else here remains design input.

- Status: Accepted; implemented for `transaction_category`
- Scope: category edits, native rules, and undo
- Evidence: [Simplifi API reference](../simplifi-api.md), including its
  [transaction rules and renaming](../simplifi-api.md#transaction-rules-and-renaming)
  section and [evidence status legend](../simplifi-api.md#evidence-status-legend)
- Implementation: `scripts/simplifi_runtime/mutations.py`, the `mutate`
  command, and the [mutation register](../mutations.md)

## Context

Provider writes can alter a live financial account, apply retroactively, or
replace an entire transaction document. A stale local snapshot, a broad match,
or a malformed partial payload can silently damage history.

## Decision

Keep scheduled and analysis workflows read-only. Mutation requires an explicit
interactive approval/commit step and is dry-run by default. A proposal file
contains decisions only; the authoritative proposal, IDs, categories, hashes,
and original values are loaded from the local store.

Before each write:

- re-fetch the live record;
- verify the proposal is still undecided and the source hash/original category
  still match;
- resolve target categories and rule definitions against the live provider;
- reject unsupported transfers, splits, or already-changed records;
- for full-document APIs, deep-copy the live document, change only the intended
  field, and send the complete document;
- preserve the exact pre-write document before sending.

Use narrow rule terms, check expected match counts and collisions across all
rows, skip existing equivalent rules, and make reruns idempotent.

Native rule mutation is additionally gated on evidence that does not yet exist.
The [Simplifi API reference](../simplifi-api.md#transaction-rules-and-renaming)
records that no rule-management endpoint was ever captured and that the matching
semantics of a contains-style operator are unverified. Both are preconditions
for this ADR's own requirements: a narrow term cannot be chosen, and an expected
match count cannot be predicted, against unknown matching semantics.

Completing that capture is necessary and not sufficient. It is deliberately
read-only — it lists rules and opens one for editing, and forbids saving,
creating or deleting — so it can settle matching semantics and the read shape
while establishing nothing about the write: no method, no payload, no response
contract. Rule mutation therefore stays out of scope until a *separately
authorized write capture* verifies those as well; a dated read-only capture
alone must not be read as unblocking it, or an implementation would end up
guessing a payload against a live financial account. Category edits on
`PUT /transactions/{id}`, whose read and write shapes are both verified, are not
blocked by any of this. Poll
asynchronous write jobs before recording success. Store before/after payloads,
responses, decisions, and resolved job IDs; rate-limit writes. Undo restores
the saved prior document where the provider still permits it, without claiming
rollback is guaranteed in every provider state.

## What is implemented

`transaction_category` is the only registered write, because
`PUT /transactions/{id}` is the only one whose endpoint, job envelope and
polling path were captured against the live app. Its *request body* was not:
the capture showed the app sending fields no GET route returns, so the document
this runtime can assemble is a strict subset of the real one on an endpoint
that replaces what it omits. Sending it is therefore gated until a write
capture settles the body; planning, preview, preconditions, audit and undo all
run regardless. Endpoint evidence and payload evidence are tracked separately
in the register for exactly this reason.

The decision boundary is preserved rather than widened. `decide` still records
judgment and still cannot authorize a write: its action vocabulary contains no
mutation, and a proposal document carries no authority. `mutate` reads the
stored decision records as *input* and requires a separate authorization naming
a person and a reason, both of which are written to the audit trail. It is
dry-run by default, refused under `--unattended`, and its preview is rendered
from the same plan objects the apply path executes.

Audit is two append-only tables. The attempt — including the complete pre-write
document — is committed before the request leaves, so a process killed mid-write
leaves an attempt with no outcome rather than no record at all. An unknown
outcome is not a failure and is never retried automatically.

Undo restores the preserved document, appends its own attempt, and refuses when
the original never settled, has already been undone, or when the provider's
copy no longer holds what the original set.

## Consequences

Accidental writes require deliberate action, stale proposals fail closed, and
partial failures leave an audit trail and a bounded recovery path. The same
approval boundary applies to one-off edits and native rule creation.

## Non-scope

This does not authorize any account-specific rule or assignment set.
