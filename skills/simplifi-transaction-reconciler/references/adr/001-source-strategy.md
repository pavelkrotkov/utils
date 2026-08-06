# ADR-001: Use a source protocol with an API-authoritative path and CSV fallback

- Status: Accepted
- Scope: transaction ingestion and provider adapters

## Context

The private API supplies stable transaction identity and the provider fields
needed for accounting semantics, projections, reference-data lookups, and
guarded writes. CSV exports are useful and portable, but omit some of those
fields and may expose only provider-renamed payees.

## Decision

Define a narrow transaction-source interface with separate adapters for the
private API, CSV, and offline fixtures. Use the API when identity, raw
descriptors, account/category IDs, pending state, scheduled-row markers,
splits, or write-back are required. Keep CSV as a first-class offline
cross-check, development fixture, and operational fallback; do not pretend it
supports capabilities its schema cannot provide.

Adapters normalize into the same internal record shape and preserve source
metadata. API pagination follows the server-supplied cursor/link verbatim,
with duplicate detection and a bounded page circuit breaker. The adapter must
fail clearly on schema or authentication errors rather than silently producing
a partial dataset.

## Consequences

Analysis can be tested without network access and can degrade to read-only CSV
work. API-only features are explicit instead of being inferred from incomplete
exports. Source selection and fetch completeness remain visible in run
metadata.

## Non-scope

This decision does not choose a credential method, define account policy, or
make provider-private endpoints a stable external contract.
