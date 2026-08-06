# ADR-004: Escalate only the residue after deterministic analysis

- Status: Accepted
- Scope: categorization, review prioritization, and optional inference

## Context

Much of the workflow is explainable from source fields, normalized identity,
accounting semantics, and observed history. Model calls add cost, latency,
privacy exposure, and failure modes; they are least trustworthy when asked to
replace missing domain evidence.

## Decision

Use this order:

1. validate the source and resolve accounting semantics;
2. apply deterministic review signals and rule-collision checks;
3. use hierarchical merchant memory only when it has sufficient observations
   and purity, otherwise preserve ambiguity;
4. send only the remaining eligible rows to an optional synchronous model;
5. emit proposals for human review, never writes.

All model backends implement one protocol. Their output must conform to the
provided taxonomy; unknown category IDs are rejected, not coerced. Prompts
contain only the minimum transaction context needed for the proposal. A model
outage produces a degraded report with an unresolved residue, not a failed
ingestion or a mutation.

Evaluate inference against both historical agreement and an independently
hand-labeled set. Track high-confidence precision, coverage, calibration,
invalid-category rate, stability, cost, and latency. Keep alternate backends
exercised if they are retained as a hedge.

## Consequences

The common path is deterministic, cheap, auditable, and usable without an LLM.
Uncertainty remains visible instead of being converted into false confidence.

## Non-scope

Thresholds, taxonomies, examples, and model/provider choices belong to the
deployment profile and evaluation data.
