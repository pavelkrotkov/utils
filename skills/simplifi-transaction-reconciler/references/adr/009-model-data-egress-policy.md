# ADR-009: Declare model egress per workflow, default to sending nothing

- Status: Accepted
- Scope: what transaction data may reach a model API, under what instruction,
  and what a person can see before it does

## Context

One workflow in this runtime can send data off the machine. `classify` posts
transactions the deterministic layers could not resolve to a model API so it
can propose categories. Every other command — `ingest`, `analyze`, `decide`,
`subs`, `probe`, `schema` — is local. (`probe` and `schema` do call the
provider API, but that is reading the user's own data from the system it
already lives in, not disclosing it to a third party.)

That property was true but unstated, and where it was enforced it was enforced
by accident:

- **The safe behaviour was the one you had to remember.** `classify` sent
  unless `--dry-run` was passed. Forgetting a flag transmitted financial data
  to a third party; there was no way to be careless in the safe direction.
- **The payload was whatever the prompt builder happened to read.**
  `build_prompt` selected five fields from a row produced by `SELECT *`, which
  carries the raw bank descriptor, the provider's transaction and account IDs,
  and a content hash. Nothing enforced that selection. A later edit adding a
  field, or a curated example quoting one, would have transmitted it silently.
- **Nothing was reviewable before the fact.** The prompt file was written
  *instead of* sending. The one run that transmitted was the one run whose
  payload nobody could inspect first, which is precisely backwards.
- **Nothing could be withheld.** The account name and the exact amount went or
  did not go along with everything else. There was no way to ask for less.

## Decision

**Every command declares its position, every run.** A local command prints
`egress: none — analyze runs entirely locally`. Saying it out loud is what
makes its absence on `classify` mean something; silence teaches a reader
nothing.

**Nothing is sent without `--send`.** Not sending is the default, and
`--dry-run` is kept only so existing scripts do not break — the two together
are an error, since one of them is now redundant and the pair reads as
confusion about which wins.

**The payload is assembled from an allowlist, never filtered down to one.**
`egress.minimize` builds each record field by field from `SENDABLE_FIELDS`
(payee, amount, account, date). The alternative — copy the row, delete what
should not go — sends every column somebody adds later and forgets to exclude.
This module is the only place that decides what may leave; `llm.build_prompt`
renders the records it is handed and cannot reach back into a database row.

**Provider identifiers are replaced with surrogates.** The model sees `t1`,
`t2`; the map back to real transaction IDs stays local. The model needs only to
tell the rows apart, while the provider's identifier would let anyone holding
both sides join this analysis to the real account. It buys nothing in exchange.
A response naming a real ID is rejected, since it could not have learned one
from the request.

**The assembled payload is checked against the rows it came from.**
`assert_payload_is_permitted` scans the finished text — after the taxonomy and
curated examples are folded in — for any value from a forbidden column. This is
what makes the allowlist a guarantee rather than a convention: it catches a
field arriving through a route nobody anticipated. A forbidden value identical
to one we are permitted to send is not a finding, or the check would fire on
every merchant whose raw descriptor equals its display name.

**Fields can be withheld or coarsened.** `--redact` accepts `account`,
`amount`, `date`. The account is dropped entirely; the amount becomes a
direction and a band; the date becomes a month. The payee is not redactable —
it is what classification reasons about, and withholding it leaves nothing to
answer. Saying so is better than accepting the flag and returning nothing
useful.

**The payload is written before it is sent, not instead.** Every run assembles
the requests, checks them, writes them to `<out>.prompt.txt` at `0600`, and
prints the path. Only then does `--send` transmit. A run can therefore be
inspected and repeated: read the file, then re-run with `--send`.

## What is sent, to whom, and for how long

**Fields.** Payee display name, amount, account name, posted date — and a
per-request surrogate ID. The payee display name is the *normalized* one: the
raw bank descriptor, which can carry card fragments, terminal IDs, and store
locations, is never sent. The category taxonomy is sent as well, since the
model must choose from it; it is a list of the user's category names and
contains no transaction.

**Destinations.** `api.openai.com` (OpenAI) for `--model luna`,
`api.anthropic.com` (Anthropic) for `--model haiku`. No other host receives
transaction data.

**Retention.** Once transmitted, data is retained under the receiving
provider's policy, not ours, and this runtime cannot delete it. That is stated
in the run output rather than buried here, because it is the part of the
decision the user cannot undo. Consult the provider's terms for their retention
and training policy before enabling `--send`.

**Locally**, the payload artifact is written at `0600` inside the data
directory, under the same rules as every other artifact (ADR-008). It is not
deleted automatically; it is a review artifact and removing it is the
operator's to decide.

**Dry behaviour.** Without `--send`, no request is made at all — not a
zero-length one, not a validation call. The command builds, checks, writes, and
returns. Tests assert this by making the send path raise if reached.

## Consequences

`classify` becomes a two-step workflow when it sends: run it, read the payload,
re-run with `--send`. That is one more step than before for the case where the
user does want a model to see their transactions, and it is the right place to
put friction.

Callers of `llm.classify` change shape: it now takes prepared payloads rather
than rows, so there is no path through it that constructs a request the caller
has not seen. `build_payloads` is the seam.

## Alternatives considered

**Keep `--dry-run` and simply document the risk.** Documentation does not
survive a forgotten flag. The default is the only control that applies when
someone is not thinking about the problem.

**Redact by pattern-matching the payload for card numbers and addresses.**
Detects what it knows to look for and quietly passes everything else. The
allowlist inverts the burden: a field is absent unless deliberately included.

**Hash or pseudonymize the payee.** Defeats the purpose — the merchant name is
the entire signal a classifier works from. The useful minimizations are the
ones that keep the payee and drop its companions, which is what `--redact` does.
