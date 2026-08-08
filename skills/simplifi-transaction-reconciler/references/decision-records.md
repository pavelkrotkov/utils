# Proposal validation and decision records

The agent boundary is a one-way pipeline:

```text
review-packet.json  →  agent judgment  →  proposals.json  →  decision records
```

`analyze` writes the [review packet](review-packet.md). An agent reads it and
writes `proposals.json`. `decide` validates that file and appends immutable
decision records to the local store:

```bash
uv run ./scripts/simplifi_transaction_reconciler.py decide \
  --db /path/to/review.sqlite \
  --packet /path/to/review-packet.json \
  --proposals /path/to/proposals.json \
  --out /path/to/decisions.json
```

Nothing in this pipeline changes a provider account. A decision record says
what a reviewer concluded and what a human should look at next; it is not an
approval and not an instruction to write.

## proposals.json

```json
{
  "document_type": "simplifi.transaction.proposals",
  "schema_version": "1",
  "packet": {
    "packet_type": "simplifi.transaction.review",
    "schema_version": "1",
    "run_id": 42,
    "analysis_date": "2026-08-15",
    "dataset_hash": "sha256…"
  },
  "reviewer": { "kind": "agent", "id": "model-or-person" },
  "proposals": [
    {
      "proposal_id": "proposal-1",
      "transaction_id": "txn-1",
      "decision": "accept",
      "action": "record_category_proposal",
      "category": "Groceries",
      "rationale": "Settled charge at a merchant whose cleared history is consistently Groceries.",
      "confidence": 0.86,
      "finding_reason_codes": ["amount_outlier"],
      "policy_references": ["ADR-002", "ADR-004"]
    }
  ]
}
```

The `packet` block is copied verbatim from the review packet that was reviewed.
`reviewer.kind` is `agent` or `human`. Required proposal fields are
`proposal_id`, `transaction_id`, `decision`, `action`, and `rationale`;
`category`, `confidence`, `finding_reason_codes`, and `policy_references` are
optional. No other field is accepted.

### Decisions and actions

`decision` is the verdict about a finding. `action` is the read-only follow-up
it implies, and only the actions that justify a verdict are accepted:

| decision   | permitted actions                                          |
| ---------- | ---------------------------------------------------------- |
| `accept`   | `record_category_proposal`, `dismiss_finding`, `none`       |
| `reject`   | `dismiss_finding`, `none`                                   |
| `escalate` | `request_human_review`                                      |
| `defer`    | `request_human_review`, `none`                              |

`record_category_proposal` records a suggested category for human review. It
does not apply one. Provider-write verbs (`apply_category`, `create_rule`,
`refresh_institution`, `update_transaction`, `undo`, and similar) are rejected
by name with the read-only boundary as the reason.

## Rejections

Validation is atomic: if any proposal is rejected, nothing is recorded and
every problem is reported with its JSON path and a code. A partially recorded
review would be a misleading audit trail.

| code                            | cause                                                        |
| ------------------------------- | ------------------------------------------------------------ |
| `unsupported_document_type`     | envelope is not a proposals document                          |
| `unsupported_schema_version`    | unsupported `schema_version`                                  |
| `unsupported_field`             | any field outside the documented allowlist                    |
| `malformed_packet_reference`    | `packet` block missing or not an object                       |
| `stale_packet_reference`        | packet type, version, dataset hash, or analysis date mismatch |
| `stale_run_reference`           | run mismatch, or the run was superseded by a later ingest     |
| `malformed_reviewer`            | missing/unknown reviewer kind or empty reviewer id            |
| `malformed_proposals`           | `proposals` missing, not an array, or empty                   |
| `malformed_proposal`            | proposal is not an object, or a list field is malformed       |
| `malformed_proposal_id`         | missing or empty `proposal_id`                                |
| `duplicate_proposal_id`         | `proposal_id` reused in one document                          |
| `unknown_transaction_id`        | `transaction_id` is not in the packet's `transaction_ids`     |
| `duplicate_transaction_id`      | more than one decision for one transaction                    |
| `malformed_decision`            | missing or unsupported `decision`                             |
| `unsupported_action`            | unknown action, or one that would change provider state       |
| `unsupported_action_for_decision` | action is not permitted for that verdict                    |
| `invalid_category`              | missing category, or one this dataset does not already use    |
| `unexpected_category`           | category supplied for an action that does not record one      |
| `missing_rationale`             | rationale missing or shorter than 12 characters of evidence   |
| `malformed_confidence`          | confidence is not a number between 0 and 1                    |

The category allowlist is the set of labels the dataset already uses, minus
account names so a transfer cannot be relabelled as its destination account. It
is deliberately broader than the classifier taxonomy: settlement state governs
what may train statistics, not whether a label exists. The runtime cannot
create a category.

A packet whose run is no longer the latest successful run is stale. Re-run
`analyze` and review the new packet rather than deciding against a snapshot the
store has moved past.

## Decision records

Accepted proposals are appended to `decision_record` in the local SQLite store
and echoed into the `--out` document — always a separate file from the input
packet, which `decide` refuses to overwrite. Each record carries:

- `decision_id`, derived from run, transaction, proposal ID, and proposal hash;
- `run_id`, `source`, `analysis_date`, and `dataset_hash` for provenance;
- `transaction_id`, `proposal_id`, and `proposal_hash` as the proposal reference;
- `decision`, `action`, `category`, and `rationale`;
- `reviewer_kind` and `reviewer_id`;
- `recorded_at` and `validator_version`.

`proposal_hash` is a SHA-256 digest over the normalized proposal, so a record
can be checked against the proposal it came from. Because `decision_id` is
derived from content, re-running `decide` with an unchanged file appends
nothing. A revised judgment has a different hash and is appended beside the
original: `decision_record` is append-only, enforced by database triggers that
reject any update or delete. History is corrected by adding to it.

`validator_version` records the rules in force when the decision was accepted,
so a stored record stays interpretable after those rules change.
