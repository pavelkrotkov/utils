# ADR-011: Derive transaction evidence once, at the source seam

- Status: Accepted
- Scope: what an adapter produces, what a consumer may read, and which facts
  are allowed to differ between sources

## Context

Two adapters produce records: the CSV export reader and the private-API reader.
Six modules consume them — prioritization, recurring analysis, merchant memory,
the HTML report, the review packet, and the model payload. Until now each
adapter assembled its own dictionary and each consumer reassembled whatever it
needed out of the raw columns.

Nothing about that arrangement crashed. That is what made it expensive. Every
failure it produced was a *plausible record that a downstream reader believed*:

- **`payee_display` meant two different things.** On the CSV path it held the
  normalizer's title-cased merchant name. On the API path it held Simplifi's
  `payee`, which for 58% of rows *is the raw bank descriptor* — `COSTCO WHSE
  #1166        NORTH PLAINFINJ` where the CSV says `Costco`. Same column, same
  reader, two kinds of value. `egress` knew this and defended itself; the
  report and the packet did not, and each re-derived a different answer.
- **`account_name` could be a provider identifier.** When an account had no
  name the API adapter wrote `accountId` into the display column. Downstream
  that was indistinguishable from a real name: the report rendered it, the
  packet published it, and the recurring-series grouping keyed on it. And the
  eligibility check *required* `account_name` — so the check was passing on the
  strength of the very substitution it looked like it was guarding against.
- **Three modules divided by 100.** Correct for USD, wrong by two orders of
  magnitude for a zero-decimal currency, and internally consistent all the way
  to the report, so no figure on the page would look out of place.

The common shape: a fact with two plausible derivations, derived independently
in several places, with nothing making the divergence visible.

## Decision

**One module owns the derivation, and both adapters go through it.**
`evidence.build_record` is the only function that writes a normalized record.
Adapters supply source facts; the seam supplies semantics — canonical merchant
identity, an account reference, currency-aware money, projection and
eligibility state, provenance. "The two adapters produce the same shape" is a
property of the code rather than of two lists somebody has to keep in step.

**Consumers read `TransactionEvidence`, not columns.** `evidence_from_row`
reconstructs it from a record or a stored row, because rows come back out of
SQLite and a type only an adapter could build would be unavailable to exactly
the consumers that need it most.

**A display name is never an identifier.** `AccountRef.display` returns the
account's name or the string `unknown account`, and never the provider's ID.
The ID stays in `account_id`, where the egress allowlist and the packet
contract already refuse it by name, and is reachable for correlation only
through `correlation_key` — a join key, never evidence.

**A provider label is a rename only when it differs from the descriptor.** When
Simplifi's `payee` equals the bank string, the provider is echoing the bank and
the normalizer's stripped output is the merchant name. `safe_display()` checks
this rather than trusting the column, because a row read back from the database
may predate the adapters agreeing on it.

**Money is the currency's, not the dollar's.** Every conversion goes through
`Money`, whose exponent comes from ISO 4217. The CSV carries no currency
column, so `ingest --currency` states it; the default is USD because that is
what this dataset is, but a zero-decimal currency has to be *sayable*, and an
amount with more precision than its currency has is refused rather than
rounded.

The exponent table lists every currency whose exponent is *not* 2, so the
default is a statement rather than a guess. A sampled table was survivable only
while the currency was hard-coded: once an operator could name one, an absent
BHD (three places) would reject a valid `1.234` and store `1.23` as BHD 0.123 —
wrong by a factor of ten, internally consistent all the way to the report.
Reading is lenient and input is strict: a stored row naming an unrecognised
code stays readable, while `--currency` refuses anything that is not a
three-letter code, because accepting a typo misscales the entire dataset.

**An unnamed account is a diagnostic, not a disqualification.** Eligibility
requires `transaction_id`, `posted_on` and `amount_minor_units`. A missing
account name adds the reason code `account_name_unknown` and the row stays
reviewable. With the ID substitution removed, requiring the name would newly
discard real transactions whose facts are all present, over a label.

## What the seam deliberately does not reconcile

Where the two sources state *different* facts, the seam makes the divergence
visible rather than papering over it.

The CSV carries Simplifi's renamed payee; the API carries the bank descriptor.
When that descriptor holds a store number and a city the rename never had,
`Costco` and `COSTCO WHSE #1166 NORTH PLAINFINJ` normalize to different
canonical keys — and no rule can invent the rename. `is_foreign_charge` is
evidence of what the descriptor carried, so only the API sees it. Settlement
state exists on the API and not in the export; report exclusion is the reverse.

`SOURCE_CAPABILITIES` states these limits and `eligibility_reason_codes`
reports them per row. A cross-source test asserts equality on the facts that
must agree and asserts *inequality* on the descriptor divergence, so that a
later change which "fixes" it by guessing fails loudly.

## Consequences

`RULESET_VERSION` moves to `0.3.0`. The same source facts can now produce a
different normalized row than they did before, and `upsert_version` compares
the ruleset version alongside the content hash — so every stored row is
re-derived on the next ingest instead of keeping a value produced by rules that
no longer exist.

Migration 014 adds `account_name_known` and backfills it, clearing any stored
`account_name` that is really the account ID. The ID itself is left in
`account_id`; the migration removes the leak, not the provenance.

Merchant-memory keys gain the account's correlation key and the row's currency.
Two unnamed accounts no longer pool into one memory, and in a mixed-currency
dataset ¥1,500 and $15.00 no longer share an amount band — they are the same
integer, and the old key could not tell them apart.

Adding a downstream consumer no longer means deciding again what a safe
merchant name is. Adding a *source* means supplying facts and declaring
capabilities, not writing a record shape.
