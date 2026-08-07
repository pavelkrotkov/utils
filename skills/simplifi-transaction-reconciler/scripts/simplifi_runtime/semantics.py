"""Accounting semantics — runs before any classification.

Transfers, card payments and balance adjustments must be identified first,
because leaving them in poisons two things at once:

  1. merchant memory learns that a bank name is a spending category
  2. amount baselines get a $4,000 card payment in the same series as $40 coffee

The Simplifi CSV gives us three usable signals: the `Exclusion` flag (30% of
rows), the category name, and the amount sign. There is no split marker or
pending/posted flag in CSV, so the adapter must report those limitations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    SPEND = "spend"
    INCOME = "income"
    TRANSFER = "transfer"
    CARD_PAYMENT = "card_payment"
    BALANCE_ADJUSTMENT = "balance_adjustment"
    INVESTMENT = "investment"
    REFUND = "refund"
    FEE = "fee"


#: Categories that are structurally not spending, regardless of sign.
NON_SPEND_CATEGORIES = {
    "transfer",
    "credit card payment",
    "balance adjustment",
}

_INVESTMENT_HINTS = re.compile(
    r"\b(investment|dividend|interest paid|redemption|core account|vigix|"
    r"reinvest|capital gain)\b",
    re.IGNORECASE,
)
_CARD_PAYMENT_HINTS = re.compile(
    r"\b(crcardpmt|cardmember|autopay|automatic payment|gsbapayment|"
    r"card\s*payment|epay)\b",
    re.IGNORECASE,
)
_FEE_HINTS = re.compile(
    r"\b(atm fee|admin fee|service charge|late fee|foreign transaction)\b", re.IGNORECASE
)
#: Income categories are nested ("Personal Income:Paycheck"), so match anywhere
#: in the path rather than at the start. Getting this wrong labels every
#: paycheck a "refund".
_INCOME_CATEGORY = re.compile(
    r"\b(income|paycheck|interest earned|dividend|reimbursement|bonus)\b", re.IGNORECASE
)


@dataclass
class Semantics:
    kind: Kind
    excluded_from_reports: bool | None
    #: True when this row must not train merchant memory or amount baselines.
    poisons_statistics: bool
    reasons: list[str]


@dataclass(frozen=True)
class SourceCapabilities:
    """Fields an adapter can authoritatively provide."""

    settlement_state: bool
    report_exclusion: bool
    stable_transaction_id: bool


SOURCE_CAPABILITIES = {
    "csv": SourceCapabilities(
        settlement_state=False,
        report_exclusion=True,
        stable_transaction_id=False,
    ),
    "api": SourceCapabilities(
        settlement_state=True,
        report_exclusion=False,
        stable_transaction_id=True,
    ),
}


@dataclass(frozen=True)
class Eligibility:
    """Whether a row can participate in general review, plus diagnostics.

    ``eligible`` is deliberately broader than ``settled``. A row with an
    unknown optional field remains visible to review, while settled-only
    statistics must still require an explicit ``CLEARED`` state.
    """

    eligible: bool
    settled: bool
    reason_codes: tuple[str, ...]


def assess_eligibility(row: dict) -> Eligibility:
    """Evaluate review eligibility without guessing missing source fields."""
    reasons: list[str] = []
    required = ("transaction_id", "posted_on", "amount_minor_units", "account_name")
    if any(row.get(field) in (None, "") for field in required):
        reasons.append("missing_required_field")

    exclusion_flag = row.get("exclusion_flag")
    if exclusion_flag is True or exclusion_flag == 1:
        reasons.append("excluded_from_reports")
    elif exclusion_flag == 2:
        reasons.append("report_exclusion_unknown")

    state = str(row.get("txn_state") or "").strip().upper()
    settled = state == "CLEARED"
    if not state:
        reasons.append("missing_optional_field")
    elif not settled:
        reasons.append("unsupported_state")

    eligible = not any(
        reason in {"missing_required_field", "excluded_from_reports"} for reason in reasons
    )
    if eligible:
        reasons.append("eligible")
    return Eligibility(eligible, settled, tuple(reasons))


def annotate_eligibility(row: dict) -> dict:
    """Attach stable, reportable eligibility fields to a normalized record."""
    result = assess_eligibility(row)
    row["review_eligible"] = int(result.eligible)
    row["eligibility_reason_codes"] = ",".join(result.reason_codes)
    return row


def classify(
    *,
    category: str,
    payee_raw: str,
    amount_minor_units: int,
    exclusion_flag: bool | None,
    account_names: set[str] | None = None,
) -> Semantics:
    """Assign an accounting kind. Deterministic, explainable, no model."""
    reasons: list[str] = []
    cat = (category or "").strip()
    cat_lower = cat.lower()
    accounts = account_names or set()

    kind: Kind | None = None

    # Simplifi encodes a transfer by setting the category to the *destination
    # account name*. That is why `CAPITAL ONE CRCARDPMT` shows up categorised as
    # `REI Co-op Mastercard`. Detect it by matching the category against the
    # known account list.
    if cat in accounts:
        kind = Kind.TRANSFER
        reasons.append("category matches an account name")
    elif cat_lower in NON_SPEND_CATEGORIES:
        kind = {
            "transfer": Kind.TRANSFER,
            "credit card payment": Kind.CARD_PAYMENT,
            "balance adjustment": Kind.BALANCE_ADJUSTMENT,
        }[cat_lower]
        reasons.append(f"category is {cat!r}")
    elif _CARD_PAYMENT_HINTS.search(payee_raw or ""):
        kind = Kind.CARD_PAYMENT
        reasons.append("payee matches card-payment pattern")
    elif _INVESTMENT_HINTS.search(payee_raw or ""):
        kind = Kind.INVESTMENT
        reasons.append("payee matches investment pattern")
    elif _FEE_HINTS.search(payee_raw or "") or cat_lower.startswith("fees & charges"):
        kind = Kind.FEE
        reasons.append("fee pattern")

    if kind is None:
        if amount_minor_units > 0:
            # A positive amount in a spending category is a refund/return, not
            # income. Income lives in its own category tree — in this dataset
            # that tree is rooted at "Personal Income", so a prefix match on
            # "income" alone is wrong. Match anywhere in the path.
            if _INCOME_CATEGORY.search(cat_lower):
                kind = Kind.INCOME
                reasons.append("positive amount in an income category")
            else:
                kind = Kind.REFUND
                reasons.append("positive amount in a spending category")
        else:
            kind = Kind.SPEND

    poisons = kind in {
        Kind.TRANSFER,
        Kind.CARD_PAYMENT,
        Kind.BALANCE_ADJUSTMENT,
        Kind.INVESTMENT,
    }
    if exclusion_flag is True and not poisons:
        # Trust the user's own exclusion flag even when our heuristics disagree.
        poisons = True
        reasons.append("user marked excluded from reports")
    elif exclusion_flag is None:
        # The API bulk read does not expose this optional capability. Preserve
        # the row for review and expose the uncertainty; absence is not proof
        # that the transaction is excluded and must not erase the dataset.
        reasons.append("report-exclusion state unavailable")

    return Semantics(
        kind=kind,
        excluded_from_reports=exclusion_flag,
        poisons_statistics=poisons,
        reasons=reasons,
    )


def is_projected(row: dict) -> bool:
    """True if this row is Simplifi's FORECAST of a bill, not a real charge.

    A scheduled-model marker separates forecast rows from real activity. A
    forecast can otherwise look like a settled charge, especially when it is
    dated in the past.

    Date is NOT a safe proxy: 11 projected rows are dated in the past.
    """
    return (row.get("txn_state") or "").upper() == "PENDING" and bool(row.get("scheduled_model_id"))


def is_real_charge(row: dict) -> bool:
    """True when a row is not a provider-generated projection.

    A real pending row can still be unsettled; use :func:`is_settled` for
    spending and recurring statistics.
    """
    return not is_projected(row)


def is_settled(row: dict) -> bool:
    """True only for activity explicitly marked ``CLEARED``.

    CSV exports do not carry settlement metadata. Treating those rows as
    settled would let an unknown pending row train memory and recurring or
    outlier statistics as if it were a confirmed charge.
    """
    return not is_projected(row) and (row.get("txn_state") or "").strip().upper() == "CLEARED"


def is_statistics_eligible(row: dict, *, allow_projected: bool = False) -> bool:
    """Whether a row is safe for statistical or learned analysis.

    Review visibility and statistical eligibility are separate. An unknown
    report-exclusion flag is retained for review but cannot train memory or
    affect baselines until the source answers that question.
    """
    if row.get("poisons_statistics") or row.get("exclusion_flag") == 2:
        return False
    return is_settled(row) or (allow_projected and is_projected(row))
