# ADR-003: Resolve accounting semantics before analysis and exclude projections from facts

- Status: Accepted
- Scope: transaction classification, statistics, and recurring analysis

## Context

Transaction feeds can mix spending with transfers, card payments, investments,
refunds, fees, balance adjustments, pending transactions, and scheduled
forecasts. Treating every row as a settled purchase poisons merchant memory,
amount baselines, duplicate checks, and recurring-charge conclusions.

## Decision

Run deterministic accounting classification immediately after ingestion and
normalization, before any learning or statistics. Use authoritative provider
kind/category fields when available, then transparent local evidence. Mark
transfers, card payments, investments, and balance adjustments as excluded from
spending statistics and merchant-memory training; preserve refunds and income
as distinct signed semantics.

Keep settled activity separate from hypotheses. A scheduled-model marker (or
the equivalent provider projection signal) identifies forecast rows; pending
alone must not be treated as a settled charge. Spending and recurring-charge
statistics use rows dated no later than the analysis date and confirmed as
cleared/real. If the source lacks the discriminator, report the limitation
instead of guessing.

Every classification stores its kind and explainable reasons so downstream
reports can show why a row was included or excluded.

## Consequences

Forecasts cannot masquerade as charges, and non-spending movements cannot teach
the classifier or distort outlier baselines. CSV and API adapters can expose
different confidence/capability levels while sharing the same semantic model.

## Non-scope

This ADR does not define account-specific category mappings, benefit rules, or
the full set of review signals.
