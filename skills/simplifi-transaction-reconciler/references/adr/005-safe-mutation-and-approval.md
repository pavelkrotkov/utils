# ADR-005: Make mutation explicit, validated, and recoverable

- Status: Accepted
- Scope: category edits, native rules, and undo

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
rows, skip existing equivalent rules, and make reruns idempotent. Poll
asynchronous write jobs before recording success. Store before/after payloads,
responses, decisions, and resolved job IDs; rate-limit writes. Undo restores
the saved prior document where the provider still permits it, without claiming
rollback is guaranteed in every provider state.

## Consequences

Accidental writes require deliberate action, stale proposals fail closed, and
partial failures leave an audit trail and a bounded recovery path. The same
approval boundary applies to one-off edits and native rule creation.

## Non-scope

This does not authorize any account-specific rule or assignment set.
