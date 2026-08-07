# Review-packet contract

`review-packet.json` is the deterministic boundary between the packaged
read/analyze runtime and agent judgment. The runtime writes it during
`analyze`, beside the HTML report by default, or at the path supplied with
`--packet-out`.

## Contract

The top-level object has `packet_type: "simplifi.transaction.review"` and
`schema_version: "1"`, followed by these fields:

```json
{
  "packet_type": "simplifi.transaction.review",
  "schema_version": "1",
  "run": {
    "run_id": 42,
    "source": "api",
    "analysis_date": "2026-08-15",
    "algorithm_version": "0.1.0",
    "ruleset_version": "0.2.0"
  },
  "source": {
    "kind": "api",
    "dataset_hash": "sha256…",
    "capabilities": {
      "stable_transaction_ids": true,
      "settlement_state": true,
      "report_exclusion": false
    }
  },
  "summary": { "transaction_count": 0, "eligible_transaction_count": 0 },
  "transaction_ids": [],
  "transactions": [],
  "excluded_transactions": [],
  "findings": [],
  "category_proposals": [],
  "limitations": [],
  "policy_references": [],
  "examples": []
}
```

The implementation validates the complete summary object before writing. The
summary also records excluded rows, findings, category proposals, unresolved
categories, and stale accounts.

Each eligible transaction contains:

- stable transaction ID, posted/transaction dates, and account display name;
- normalized merchant identity (`canonical`, `normalized`, and `display`);
- signed minor-unit amount, currency, and exponent;
- category and accounting kind, transaction/match state, and review flags; the
  flags explicitly identify provider-generated projected rows;
- eligibility/accounting reason codes; and
- append-only provenance (`transaction_version_id`, run ID, source hash, and
  algorithm/ruleset versions).

Each deterministic finding identifies its transaction or merchant-series scope,
the contributing `transaction_ids`, priority, reason codes, evidence, and
applicable ADR references. Merchant-series findings use normalized merchant
names and stable member transaction IDs; internal account identities are never
serialized. Monetary finding evidence is represented with minor units, currency,
and exponent. Deterministic findings intentionally set probabilistic
`confidence` to `null`; their evidence, not a made-up probability, is what the
agent should evaluate.
Category proposals carry confidence only when deterministic merchant memory
provided the proposal; unresolved rows carry `null`.

`examples` is an explicit input to packet construction and is checked against a
small safe field allowlist. The current runtime does not promote transaction
history into that field. A later judgment-context layer may supply only
deliberately curated and sanitized examples from
`references/examples/judgment-examples.md`; account IDs, transaction IDs,
account names, source hashes, and raw descriptors are rejected.

## Safety and reproducibility

The packet is read-only and contains no provider mutation instructions. Raw
statement descriptors, account IDs, source paths, API responses, cookies,
passwords, access tokens, and other credential fields are excluded. Missing or
unresolved account lookup names become `unknown account`. The
dataset identity is a SHA-256 digest of sorted transaction/source hashes, not a
CSV path or a raw source payload.

Packet lists are sorted by stable identifiers or reason codes, and JSON is
written with sorted keys and no generation timestamp. Identical run inputs
therefore produce identical bytes. `write_packet` always validates before
crossing the file boundary.
