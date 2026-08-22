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
applicable ADR references. Deterministic findings intentionally set
probabilistic `confidence` to `null`; their evidence, not a made-up
probability, is what the agent should evaluate.

Merchant-series findings are transcriptions of the recurring-analysis result,
not a second derivation of it. Their evidence carries:

- `kind` — one of `zombie`, `hike`, `twin`, `renamed`, `ghost`, `lapsed`;
- `series[]` — one entry per contributing series, each with the normalized
  `merchant`, the account's display name, its member `transaction_ids`, its
  `monthly` cost, `interval_days`, and `last_charge`;
- `annual_impact` — the yearly effect, signed (a lapsed series is a saving);
- `amounts` — kind-specific money facts, e.g. `previous`/`current` for a hike,
  `projected_charge` for a ghost;
- `facts` — kind-specific non-money facts, e.g. `silent_days`, `ratio`,
  `shared_token`, `charges_after_schedule`;
- `detail` — the same information as a sentence, for a human reader.

Internal series keys and provider account identities are never serialized: the
key that separates two people billed by the same merchant is built from the
provider's account ID, and it exists to join rows, not to be read. Every
monetary value — in `annual_impact`, in `amounts`, and in each series' `monthly`
— is minor units, currency, and exponent, in the currency of the series itself.
Category proposals carry confidence only when deterministic merchant memory
provided the proposal; unresolved rows carry `null`.

`examples` contains the small, explicitly promoted set selected from
`references/examples/judgment-examples.md`. The loader accepts only the five
portable decision headings, rejects sensitive field names, and ranks examples
by deterministic topic overlap with the current findings. It never reads
transaction history to populate this field. The same curated examples are
rendered into the optional classifier prompt as general guidance; they are not
few-shot copies of a user's transactions. Account IDs, transaction IDs,
account names, source hashes, and raw descriptors are rejected.
The runtime also carries a copy beside the scripts so the read/analyze entry
point remains usable when only the script cluster is deployed.

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

## Answering the packet

Agent judgment returns through `proposals.json`, validated by `decide` and
recorded as append-only decision records. See the
[decision-record contract](decision-records.md) for that half of the boundary.
The packet's `run_id`, `analysis_date`, and `dataset_hash` are what a proposal
document must echo back, so a review of a superseded run fails closed.

## Contract enforcement

The contract is an allowlist at every level, not a presence check. Transactions,
findings, category proposals, excluded rows, and examples each declare exactly
which fields they may carry, which are required, and what type each holds. A
field is in the contract or it is not in the packet.

- **Money is validated as money**: three fields, an integer minor-unit count, a
  non-empty currency, and a plausible ISO 4217 exponent. A bare number is
  rejected rather than coerced — 1500 is ¥1,500 and $15.00 at once, and a
  reader holding only the number cannot tell which.
- **Every flag must be present and boolean**, not only `projected`. An omitted
  flag reads as `False` to a consumer using `.get`, so a forecast that lost its
  marker would be published as a real charge.
- **Values are checked against their source rows.** `assert_no_sensitive_values`
  refuses a raw descriptor, provider ID, or pre-conversion amount that appears
  anywhere in the finished document, including under a permitted key — a
  descriptor arriving as `merchant.display` passes every structural check. A
  value that is also one the row may publish is not a finding, because a
  descriptor with nothing to strip *is* its own merchant name.
- **The HTML report renders through `transaction_view`**, the same projection
  the packet uses, so the two artifacts cannot describe one transaction
  differently. Recurring findings work the same way: both artifacts read the
  analysis result object, so neither re-derives an annual impact or re-guesses
  a currency, and they cannot state one figure two ways.

Validation runs before the file boundary, and `write_packet` writes atomically
— a reader sees the previous packet or the new one, never a partial.

See [ADR-012](adr/012-read-only-output-seam.md).
