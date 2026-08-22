# Mutation capability and risk register

The packaged runtime can change a live financial account through exactly one
capability. This file is the register: what each considered mutation would do,
how its behavior is known, what it risks, and — where it is refused — why.

`mutate --capabilities` prints the same register from the code, so the two
cannot drift.

## Contents

- [What is available](#what-is-available)
- [What is refused, and why](#what-is-refused-and-why)
- [The write path](#the-write-path)
- [Authorization](#authorization)
- [Preconditions](#preconditions)
- [Audit trail](#audit-trail)
- [Undo](#undo)

## What is available

| Capability | Endpoint | Endpoint evidence | Payload evidence | Blast radius | Reversible |
|---|---|---|---|---|---|
| `transaction_category` | `PUT /transactions/{id}` | Verified 2026-08-04/05 | **Unverified — writes gated** | one transaction | yes |

**Risk.** It is a full-document write, not a PATCH. Every field in the request
replaces the provider's copy, so a document assembled from anything other than
the live one silently reverts whatever it omits. The category itself affects
budgets, reports and any rule keyed on it, retroactively.

### Why the write is gated

The endpoint is verified and the payload is not, and those are separate
questions. Conflating them is how a guessed body reaches a live account.

The capture recorded the PUT *body* carrying `memo`, `split`, review and
exclusion flags, `isSubscription` and `cpData` — and no GET route returns any of
them. This runtime can only assemble a document from what GET serves, so what it
would send is a strict subset of what the app sends, on an endpoint that
replaces whatever it omits.

So `mutate --apply` refuses, naming the gap, while everything up to the send
runs: planning, preconditions, the live re-fetch, the dry run, the audit schema
and undo are all exercised end to end against fixtures. The plan the dry run
prints is exactly what would be sent once the payload is known.

**What unblocks it:** a capture of one real category change made in the web app,
recording which fields the request body carries and what the provider does with
those it omits. That is a write capture, so it needs its own authorization —
it is not part of the read-only rule-capture procedure.

## What is refused, and why

| Capability | Refused because |
|---|---|
| `transaction_rule` | No rule endpoint was ever captured, and the semantics of a contains-style match operator are unverified. ADR-005 requires a narrow term and a predicted match count; neither can be produced against unknown matching. See [transaction rules and renaming](simplifi-api.md#transaction-rules-and-renaming). |
| `institution_refresh` | The shape is verified but the in-band MFA channel was never exercised. A refresh can present an institution challenge, and there is no interactive path to answer one. |
| `transaction_delete` | Never captured, and nothing in the read-only workflow produces a finding whose remedy is deleting a transaction. It would destroy history no local record can rebuild. |

A refusal that carries its reason is a decision that stays decided. Removing an
entry from this table is not how a capability becomes available; capturing the
evidence is.

## The write path

0. Confirm the capability's payload is verified. `transaction_category`'s is
   not, so steps 3–6 are currently refused; steps 1 and 2 still run in full.
1. Build a plan from **stored decision records**, never from a proposal file.
   The category to set, the transaction ID, and the original value are read out
   of the local store. A proposal document carries judgment, not authority.
2. Print the plan. This is the default: without `--apply` nothing is sent, and
   the preview is rendered from the same plan objects the apply path executes.
3. Re-fetch the live document, check it against the plan, refuse if it moved.
4. Deep-copy the live document, change only `coa`, send the whole thing.
5. Poll `/job-statuses/{id}` until the job settles. A `200` on the `PUT` means
   the provider accepted the request, not that it did the work.
6. Record the outcome.

## Authorization

`--apply` alone is not enough. A write requires `--authorized-by NAME` and
`--authorization-note "why"`, both of which are written to the audit trail.

A boolean flag records that something was true at some point, which nobody
reading the trail months later can distinguish from a flag left in a shell
history or a cron line. A name and a sentence can be read back and disputed.

`--apply` is refused outright under `--unattended`. An authorization names a
person and a scheduled run has none; ADR-010 keeps periodic execution
read-only, and this check makes putting `mutate --apply` in a cron line fail at
the first firing rather than in review.

## Preconditions

A mutation is skipped, before anything is sent, when:

- the decision was recorded about a superseded run;
- the transaction has been retired since the review;
- the transaction is not a current row;
- the row already holds the target category;
- the decision has already been applied, or applied with an unknown outcome.

And it is refused, after the live re-fetch, when the provider's copy no longer
holds the category the plan recorded — someone changed it in the app, and the
review was about a different fact.

An unknown outcome counts as applied. A write that left and never came back may
have landed, and the safe reading of "we do not know" is not "do it again".

## Audit trail

Two append-only tables, enforced by trigger.

`mutation_attempt` is written and committed **before** the request leaves. It
holds the capability, the transaction, the authorizing decision and run, who
authorized it and why, and the complete before/after documents as JSON.

`mutation_outcome` is written when the provider answers: `succeeded`, `failed`,
or `unknown`.

An attempt with no outcome row is a write that left and never settled. That
state is recorded by construction rather than by a dying process remembering to
write it down — which is exactly the moment a single-row design would record
nothing at all.

## Undo

Undo is implemented for `transaction_category` and is a **restore, not a
rollback**. It writes back the document the provider served immediately before
the change, so any other edit made in the app since then is overwritten too.
That is what a full-document API makes possible, and it is stated rather than
hidden.

An undo is itself a mutation: it needs its own authorization, appends its own
attempt pointing back at the one it reverses, and can fail. Nothing about the
original row changes, because the original write really did happen.

It refuses when:

- the original never settled — undoing a write that may not have landed writes
  a guess;
- the original already has an undo;
- the provider's copy no longer holds what the original set, which means
  somebody else changed it and undoing would overwrite their work.

See [ADR-005](adr/005-safe-mutation-and-approval.md).
