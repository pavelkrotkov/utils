# ADR-012: One output seam for every agent-facing artifact

- Status: Accepted
- Scope: the read-only output path — what the packet, the report, and the model
  payload may say, how the contract is enforced, and how artifacts are written

## Context

ADR-011 gave the *input* side one derivation: adapters produce normalized
evidence, and consumers read it rather than re-deriving facts from columns.
This decision is the other half. Three artifacts leave this runtime for a
reader who is not the code that produced them — the review packet, the HTML
report, and the model payload — and each still selected and formatted its own
fields on the way out.

That leaves four gaps, and none of them is a crash.

**Two artifacts from one run could disagree.** The packet and the report were
generated from the same rows by different code. A reader comparing them has no
way to tell which is right, and the report is the one a person actually acts
on. The report also rendered a projection identically to a real charge, while
the packet had always carried `flags.projected` — so the artifact that could
tell them apart was the one nobody reads.

**The contract validated containers, not contents.** `validate_packet` checked
that `transactions` was an array and that each element had certain keys. It did
not check what those keys held, and it did not check what *else* the element
held. A transaction carrying `payee_raw` alongside the required fields passed
every structural check; only a separate forbidden-key scan caught it, which
means a sensitive field nobody had thought to name would have travelled.

**Nothing checked values.** The key-name scan cannot catch a raw descriptor
that arrives as `merchant.display`, or a provider account ID rendered as
`account_name`. Those sit under permitted keys. `egress` had solved exactly
this problem for the model payload, and the packet — the other agent-facing
artifact — had no equivalent.

**Writes could destroy what they were reporting on.** `analyze` compared the
report against the packet and the packet against the database, but never the
report against the database: `--out simplifi.sqlite` truncated the ledger
*after* the analysis read it, so the command printed what it found and
destroyed the evidence on the way out. The comparisons used `Path` equality, so
`./db` and `db` were different files to the check and one file to the kernel.
And every write truncated first, so a render that raised halfway left a file
that was neither artifact — not missing, which someone would notice, but
*short*, which reads as a real report that found nothing.

## Decision

**The report renders through the packet's projection.**
`review_packet.transaction_view` is public, and the report's transaction-bearing
rows go through it. Two artifacts that each select and format their own fields
will eventually disagree about one; deriving one from the other's projection
makes the disagreement unrepresentable rather than merely unlikely. The report
now shows the projection flag it always had access to.

**The contract is an allowlist, at every level.** Transactions, findings,
proposals, excluded rows, and examples each declare exactly which fields they
may carry, which are required, and what type each holds. A field is in the
contract or it is not in the packet — which inverts the burden from "did we
remember to forbid this?" to "did we decide to allow it?".

Money is validated as money: three fields, an integer minor-unit count, a
non-empty currency, and a plausible ISO 4217 exponent. Rejected, never coerced
— a packet whose amount is a bare number has already lost the distinction the
runtime is built on, because 1500 is both ¥1,500 and $15.00 and a reader with
only the number cannot tell which they were handed. Every flag must be present
and boolean, not only `projected`: an omitted flag reads as `False` to any
consumer using `.get`, so a forecast that lost its marker would be presented as
a real charge.

**Values are checked against the rows they came from.**
`assert_no_sensitive_values` scans the finished packet for each row's raw
descriptor, provider IDs, and pre-conversion amounts, and refuses a match that
is not covered by something the row is entitled to publish. "Covered" means
equality, except for `original_amount` — a foreign charge's `2.90` sits inside
the issuer-converted `-2.90` the packet states, and refusing that would fail
every foreign transaction over a value present only because the amount is.
Widening the exemption to the identifiers is how one escapes: an account
genuinely named `Checking acct-99887766` would make its own provider ID a
substring of a publishable value. Equality is all the real case needs, since a
descriptor with nothing to strip *equals* its merchant name and a stripped name
is shorter than its descriptor, never longer. Modelled on
`egress.assert_payload_is_permitted` deliberately: the packet and the payload
are two agent-facing artifacts, and a packet that refused less than the payload
would be the softer of two doors into the same room.

**Outputs are reserved before anything is opened.**
`artifacts.reserve_outputs` takes every path a command will write plus every
path it reads, normalizes them, and refuses any collision by name. One check
over all of them, rather than the pairs somebody remembered.

**Artifacts are written whole or not at all.** `atomic_open` writes to a
temporary file in the target's own directory, fsyncs, and renames. A reader
sees the previous artifact or the new one, never a partial. The temporary
carries owner-only permissions from creation and is removed if *anything*
fails, the final hardening and rename included — those run after the caller's
last write, so a target that turns out to be a directory is exactly the case
that leaves a fully-written temporary behind. Its name carries random bytes as
well as the PID, so one leaked temporary cannot become a permanent refusal for
every later process that reuses that PID against that target.

**Case is decided by the filesystem, not assumed.** On a case-insensitive
volume — a default macOS install — `report.html` and `REPORT.HTML` are two
`PosixPath` values and one directory entry, so `analyze` would write the packet,
replace it with the report, exit zero, and claim it had produced both. A probe
per directory answers the question; a probe that cannot run answers
"case-sensitive", which refuses nothing, because guessing the strict way would
block a valid run on a volume where the two really are different files.

## Consequences

The output path stays read-only. Nothing here adds a provider mutation, and the
packet remains a terminal artifact carrying findings and proposals rather than
write instructions.

Determinism is now testable across artifacts rather than within one. The tests
compare the packet, the report, and the model payload for the same row across
five shapes — ordinary, a legacy row whose stored display is really the
descriptor, an unnamed account, a zero-decimal currency, and a projection — and
assert they state one merchant name and one amount at one precision.

A new packet field is now a deliberate act: it has to be added to the
allowlist, given a type, and given a reason. That is the cost, and it is the
point.
