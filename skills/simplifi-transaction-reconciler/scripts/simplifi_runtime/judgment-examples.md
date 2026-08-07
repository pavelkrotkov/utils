# Judgment examples

These cases are bundled with the runtime so the script cluster remains usable
when copied without the surrounding skill references.

## 1. Projected versus real subscription

**Situation**

A recurring merchant appeared to have many more charges than the account
history supported.

**Evidence**

Some rows were already posted and cleared. The others were future-dated
scheduled rows. A separate recent row was pending.

**Proposal or escalation**

Count only posted, cleared activity in spending and subscription statistics.
Keep projections as forecast evidence, and do not treat a pending row as a
settled charge.

**Human decision**

Review the real charges now; leave the projected schedule out of historical
spending and revisit the pending charge after it clears.

**Reusable lesson**

A forecast is a hypothesis, not a transaction. Separate cleared rows from
scheduled projections and pending activity before reporting counts or totals.

## 2. Statement evidence versus display name

**Situation**

Several small EV-charging charges appeared under a display name that suggested
phone service.

**Evidence**

Richer source evidence identified a mobility provider, and the amounts were
inconsistent with a phone bill. The renamed display value came from merchant
inference, not a user rule. The CSV exposed the renamed payee but not the
richer identity evidence.

**Proposal or escalation**

Inspect richer statement evidence, then emit a narrow correction proposal and
classify the charges as EV charging.

**Human decision**

Use statement evidence as the identity signal, report the likely mobility
provider, and treat the charges as fuel/EV charging rather than phone service.
Keep the rename and category change as a proposal; the workflow does not apply
provider changes.

**Reusable lesson**

Display names can already contain the error under review. Prefer richer
statement identity evidence for matching and diagnosis, use amounts and context
as a sanity check, and do not infer rule causes from a renamed export alone.

## 3. Merchant rebrand and rule drift

**Situation**

The fitness vendor changed brands. New charges silently moved from the fitness
category to restaurant spending.

**Evidence**

Older rows were categorized correctly. Later descriptors were not caught by
the old merchant pattern. Multiple rows were affected, with no application
error or missing-charge alert.

**Proposal or escalation**

Compare descriptor changes against category history, then propose stable
vendor-based coverage after confirming that the rebrand is the same vendor.

**Human decision**

Treat the new descriptors as fitness charges and recommend correcting the
affected history, with future coverage based on a stable vendor token rather
than variable customer text. Record outcomes as proposals and do not apply
them.

**Reusable lesson**

Rule health is not just syntax or match counts. Detect merchant identity drift
when a familiar recurring charge changes descriptor, because silent fallback
categorization can distort totals without failing loudly.

## 4. High-value merchant needs human confirmation

**Situation**

The year's largest discretionary charge was filed under Travel even though
merchant evidence pointed to home equipment.

**Evidence**

The payment processor and merchant evidence point to home equipment, but a
processor-fronted, high-value charge also warrants checking the amount against
the receipt or order.

**Proposal or escalation**

Escalate the amount and merchant identity for human confirmation before a
retroactive category change. If confirmed, propose Home Improvement.

**Human decision**

The charge was confirmed as the expected home-equipment purchase and accepted
as Home Improvement.

**Reusable lesson**

High-value or processor-fronted transactions need an amount or receipt check
before finalizing a proposal, even when the likely category is obvious.

## 5. Statement credits are neither spending nor income

**Situation**

Two positive card-benefit rows looked like income or refunds, and one appeared
near a related purchase.

**Evidence**

The credits exactly matched the annual card-benefit allowance when combined.
One partially reimbursed an earlier purchase; the credit is a card benefit
tied to an allowance, not a new source of cash.

**Proposal or escalation**

Track the benefit against its allowance cycle and place the inflows in a
dedicated statement-credit category. Do not classify them as spending or
ordinary income; verify the allowance cap against card documentation because
the observed cap was inferred from the transactions.

**Human decision**

Record the rows as Statement Credits and track used/remaining allowance
separately from spending and income totals.

**Reusable lesson**

Positive transaction sign does not determine accounting meaning. Model card
benefits and reimbursements explicitly so credits offset the relevant benefit
or allowance without inflating income or disguising spending.
