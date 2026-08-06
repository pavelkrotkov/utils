# ADR-002: Preserve descriptor provenance and group by canonical merchant identity

- Status: Accepted
- Scope: payee identity, normalization, and matching

## Context

A provider may expose a raw statement descriptor, a cleaned/inferred name, and
a display value. Display names can change or merge/split merchants, while raw
descriptors contain processor noise that makes grouping unstable. Destroying
the raw value makes normalization errors and provider-rule matching difficult
to diagnose.

## Decision

Represent merchant identity as four distinct values:

1. `raw`: the source descriptor, unchanged;
2. `normalized`: deterministic cleanup of processor noise;
3. `canonical`: stable lowercase key used for grouping and memory;
4. `display`: human-facing label.

Record every normalization rule that fired. Group analysis, memory, recurring
series, and collision checks by `canonical`, never by `display`. Keep any
currency or amount text found inside a descriptor as descriptive metadata only;
the transaction amount and currency come from the source/account fields.

When creating provider-native matching rules, prefer the provider's raw
statement-name field. Generate narrow, evidence-backed terms and test them
against the complete dataset, including non-spending rows.

## Consequences

Normalization is deterministic, offline-testable, reversible for diagnosis, and
safe to improve without losing source evidence. Merchant-level decisions remain
stable across display renames, while rule matching uses the field the provider
actually matches.

## Non-scope

The normalization rules and merchant mappings are deployment/profile data, not
portable defaults in the skill.
