# Judgment examples

These compact cases preserve decisions that should shape future reconciliation
reviews. They are examples of reasoning, not portable transaction data or a
copy of the current rule set.

## 1. Projected versus real subscription

**Situation**

A recurring merchant appeared to have 17 charges, making the subscription
look much more active than the account history supported.

**Evidence**

Four rows were already posted and cleared. The other 13 were future-dated
scheduled rows extending into 2027. A separate recent row was pending.

**Proposal or escalation**

Count only posted, cleared activity in spending and subscription statistics.
Keep projections as forecast evidence, and do not treat a pending row as a
settled charge.

**Human decision**

Review the four real charges now; leave the projected schedule out of historical
spending and revisit the pending charge after it clears.

**Reusable lesson**

A forecast is a hypothesis, not a transaction. Any recurring-charge detector
must separate `posted_on <= today` and cleared rows from scheduled projections
and pending activity before reporting counts or totals.

## 2. Raw descriptor versus display name

**Situation**

Several small Spanish EV-charging charges appeared under the display name
`T-mobile`, which suggested roaming or phone service.

**Evidence**

The raw statement descriptor was `IBERDROLA SMART MOBILITY`; the amounts were
also inconsistent with a phone bill. The renamed display value came from
Simplifi's merchant inference, not a user rule. The CSV exposed the renamed
payee but not the original descriptor.

**Proposal or escalation**

Inspect the raw statement field through the richer source, then emit a narrow
statement-based correction proposal and classify the charges as EV charging.

**Human decision**

Use the raw descriptor as the identity evidence, report Iberdrola as the likely
merchant, and treat the charges as fuel/EV charging rather than T-mobile. Keep
the rename and category change as a proposal; this read-only workflow does not
apply provider changes.

**Reusable lesson**

Display names are outputs that can already contain the error under review.
Prefer raw descriptors for matching and diagnosis; use amounts and context as
a sanity check, and do not infer rule causes from a post-rename export alone.

## 3. Merchant rebrand and rule drift

**Situation**

The fitness vendor changed from Gympass to Wellhub. New charges silently moved
from the fitness category to restaurant spending.

**Evidence**

Older `Gympass US LLC` rows were categorized correctly. Later descriptors such
as `Wellhub <person's name>` were not caught by the old merchant pattern. Ten
rows totaling $693 were affected, with no application error or missing-charge
alert.

**Proposal or escalation**

Compare descriptor changes against category history, then propose stable
Wellhub-based coverage after confirming that the rebrand is the same vendor.

**Human decision**

The historical human decision was to treat the new descriptors as fitness
charges and recommend correcting the affected history, with future coverage
based on the stable vendor token rather than each person's name. In the current
read-only workflow, record those outcomes as proposals and do not apply them.

**Reusable lesson**

Rule health is not just syntax or match counts. Reconciliation should detect
merchant identity drift when a familiar recurring charge changes descriptor,
because silent fallback categorization can distort totals without failing
loudly.

## 4. High-value merchant needs human confirmation

**Situation**

`AWX*Mammotion Technology` was the year's largest discretionary charge,
$2,611.25, but it had been filed under Travel.

**Evidence**

`AWX*` identifies a payment processor and Mammotion sells robotic mowers; the
merchant evidence points to home equipment, but a processor-fronted,
high-value charge also warrants checking the amount against the receipt or
order.

**Proposal or escalation**

Escalate the amount and merchant identity for human confirmation before a
retroactive category change. If confirmed, propose Home Improvement.

**Human decision**

The charge was confirmed as the expected robotic-mower purchase and accepted
as Home Improvement.

**Reusable lesson**

Confidence is not only merchant classification. High-value or processor-fronted
transactions need an amount/receipt check before finalizing a proposal, even
when the likely category is obvious.

## 5. Statement credits are neither spending nor income

**Situation**

Two positive `Elite Lifestyle Credit` rows looked like income or refunds and
one appeared near an ESPN purchase.

**Evidence**

The credits were $31.98 and $118.02, exactly $150.00 together—the annual card
benefit allowance. The first partially reimbursed an earlier ESPN charge; the
credit is a card benefit tied to an allowance, not a new source of cash.

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
