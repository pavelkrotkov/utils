"""The source seam: one normalized transaction record, whatever the source.

Before this module every adapter assembled its own dictionary and every
consumer reassembled the facts it needed out of the raw columns. Two costs
followed from that, and both were being paid.

**The adapters disagreed about what a field meant.** `payee_display` held the
normalizer's title-cased merchant name on the CSV path and Simplifi's `payee`
on the API path — which for 58% of API rows *is the raw bank descriptor*,
"COSTCO WHSE #1166 NORTH PLAINFINJ" where the CSV says "Costco". Same column,
same downstream reader, two different kinds of value. `account_name` had the
same problem in a sharper form: the API adapter fell back to `accountId` when
an account had no name, so a provider identifier travelled under a display
name's label, through every consumer that trusted the label.

**Every consumer re-derived the same facts, differently.** Three modules turned
minor units into a printable amount by dividing by 100 — correct for USD, wrong
by two orders of magnitude for a zero-decimal currency. Two decided
independently what a "safe" merchant name was. `egress` had to defend itself
against `payee_display` because it could not trust it, and its defence was a
comment explaining why the field it was handed might be poison.

So the derivation happens once, here, at the point where a source becomes a
record. Adapters supply source facts; this module supplies the semantics:
canonical merchant identity, an account reference that never carries a provider
ID, currency-aware money, projection and eligibility state, and provenance.
Consumers read `TransactionEvidence`, not columns.

What deliberately does *not* move here: provider IDs and raw descriptors stay in
the record for provenance, hashing and duplicate detection, and are reachable
only through the explicitly-named internal accessors. Nothing agent-facing
should ever call those.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .money import Money, from_decimal, money_from_row
from .normalize import Descriptor, normalize
from .semantics import (
    Semantics,
    annotate_eligibility,
    classify,
    is_projected,
    is_settled,
    is_statistics_eligible,
    is_statistics_quarantined,
)

#: What an account is called when the source could not name it. A constant
#: rather than a literal in four renderers, because the packet contract, the
#: report and the tests all have to agree on the exact string for a reader to
#: recognise it as "we don't know" rather than as somebody's account.
UNKNOWN_ACCOUNT = "unknown account"

#: Same, for a merchant whose descriptor normalized away to nothing.
UNKNOWN_MERCHANT = "unknown merchant"


class EvidenceError(ValueError):
    """A source fact that cannot be turned into normalized evidence."""


@dataclass(frozen=True)
class MerchantIdentity:
    """Who was paid, in the four forms the pipeline keeps distinct.

    `raw` is retained because duplicate detection and the source hash need the
    exact string the bank sent, and because a normalization that goes wrong is
    only debuggable against its input. It is not display material and no
    agent-facing path may render it.
    """

    raw: str
    normalized: str
    canonical: str
    display: str
    rules_applied: tuple[str, ...] = ()
    #: The pre-conversion charge a foreign descriptor carried, when it carried
    #: one. NOT the transaction's currency — the issuer already converted, and
    #: the settlement amount is authoritative. See `normalize.Descriptor`.
    original_currency: str | None = None
    original_amount: str | None = None

    @property
    def is_foreign_charge(self) -> bool:
        return self.original_currency is not None

    @property
    def is_renamed(self) -> bool:
        """True when a human, not the normalizer, chose the display name."""
        return bool(self.display) and self.display != self.raw and self.display != self.normalized

    def safe_display(self) -> str:
        """A merchant name fit for a report, a packet or a model payload.

        Never the raw descriptor unless normalization found nothing to strip —
        which is exactly the case where the descriptor *is* the merchant name
        and there is nothing left to protect.

        `display` is checked against `raw` rather than trusted, because a row
        read back from the database may predate the adapters agreeing on this
        field and can still hold the provider's echo of the bank descriptor.
        A display value that IS the raw string is not a display value.
        """
        raw = self.raw.strip()
        display = self.display.strip()
        if display and display != raw:
            return display
        normalized = self.normalized.strip()
        if normalized and normalized != raw:
            return normalized
        return display or normalized or raw or UNKNOWN_MERCHANT


def merchant_identity(raw: str, provider_label: str | None = None) -> MerchantIdentity:
    """Normalize a descriptor, honouring a provider rename when there is one.

    `provider_label` is Simplifi's own `payee`. It is only a rename when it
    differs from the descriptor; when the two are equal the provider is simply
    echoing the bank, and using it as a display name would put the store number
    and the terminal location on the screen and in the payload. That decision
    lives here so that no consumer has to guess which of the two it was handed.
    """
    descriptor: Descriptor = normalize(raw or "")
    label = (provider_label or "").strip()
    renamed = bool(label) and label != (raw or "").strip()
    return MerchantIdentity(
        raw=raw or "",
        normalized=descriptor.normalized,
        canonical=descriptor.canonical or "unknown",
        display=label if renamed else descriptor.display,
        rules_applied=tuple(descriptor.rules_applied),
        original_currency=descriptor.original_currency,
        original_amount=descriptor.original_amount,
    )


@dataclass(frozen=True)
class AccountRef:
    """Which account, separating what may be shown from what may not.

    The provider's account ID is kept for correlation — refund matching and
    recurring-series grouping both need a stable per-account key, and a display
    name is not one. It is reachable only as `provider_id`, and `display` never
    falls back to it. That fallback is what this type exists to make
    impossible: it previously put an opaque provider identifier in front of a
    reader as though it were the name of their checking account, and every
    consumer downstream believed the label.
    """

    name: str = ""
    provider_id: str | None = None

    @property
    def is_named(self) -> bool:
        return bool(self.name)

    @property
    def display(self) -> str:
        """Always safe, always non-empty, never an identifier."""
        return self.name or UNKNOWN_ACCOUNT

    @property
    def correlation_key(self) -> tuple[str, str] | None:
        """A stable key for grouping one account's rows, or None if there is none.

        Prefers the provider ID because two accounts can share a name and one
        account can be renamed between runs. Internal only — this is a join
        key, not evidence, and must not reach an artifact.
        """
        if self.provider_id:
            return ("id", self.provider_id)
        return ("name", self.name) if self.name else None


def account_ref(row: Mapping[str, Any]) -> AccountRef:
    """Recover the account reference from a stored or normalized record.

    Tolerates rows written before `account_name_known` existed, where the API
    adapter's ID fallback may still be sitting in `account_name`: a name equal
    to the ID is not a name.
    """
    name = str(row.get("account_name") or "").strip()
    provider_id = str(row.get("account_id") or "").strip() or None
    known = row.get("account_name_known")
    if (known is not None and not known) or (provider_id and name == provider_id):
        name = ""
    return AccountRef(name=name, provider_id=provider_id)


@dataclass(frozen=True)
class Provenance:
    """Where a row came from and under which derivation it was produced."""

    source: str = ""
    transaction_version_id: int | None = None
    run_id: int | None = None
    source_hash: str = ""
    algorithm_version: str = ""
    ruleset_version: str = ""


@dataclass(frozen=True)
class TransactionEvidence:
    """One transaction, as everything downstream of the adapters should see it.

    Constructed from a record dict rather than replacing it: rows are read back
    out of SQLite, and a dataclass that could only be built by an adapter would
    be unavailable to exactly the consumers that need it most.
    """

    transaction_id: str
    posted_on: str
    transacted_on: str | None
    merchant: MerchantIdentity
    account: AccountRef
    money: Money
    kind: str
    category: str
    inferred_category: str | None
    transaction_state: str
    match_state: str
    review_eligible: bool
    reason_codes: tuple[str, ...]
    projected: bool
    settled: bool
    statistics_eligible: bool
    statistics_quarantined: bool
    uncategorized: bool
    recurring: bool
    split: bool
    reviewed: bool
    foreign_charge: bool
    provenance: Provenance

    @property
    def currency(self) -> str:
        return self.money.currency

    def amount_evidence(self, minor_units: int | None = None) -> dict[str, Any]:
        """A money fact in the one shape every artifact should carry.

        Both units travel together on purpose. The major-unit float is what a
        person reads; the integer is what a later run compares against, and a
        figure recorded only as a float cannot be compared exactly.
        """
        money = self.money if minor_units is None else Money(int(minor_units), self.money.currency)
        return {
            "minor_units": money.minor_units,
            "currency": money.currency,
            "currency_exponent": money.exponent,
            "amount": money.as_float,
        }


def evidence_from_row(row: Mapping[str, Any]) -> TransactionEvidence:
    """Read normalized evidence out of a record or a stored row.

    The single entry point for consumers. Anything that reaches past this into
    the raw columns is re-deriving semantics this module already decided.
    """
    plain = dict(row)
    codes = tuple(
        code.strip()
        for code in str(plain.get("eligibility_reason_codes") or "").split(",")
        if code.strip()
    )
    return TransactionEvidence(
        transaction_id=str(plain.get("transaction_id") or ""),
        posted_on=str(plain.get("posted_on") or ""),
        transacted_on=(str(plain["transacted_on"]) if plain.get("transacted_on") else None),
        merchant=MerchantIdentity(
            raw=str(plain.get("payee_raw") or ""),
            normalized=str(plain.get("payee_normalized") or ""),
            canonical=str(plain.get("payee_canonical") or "") or "unknown",
            display=str(plain.get("payee_display") or ""),
            rules_applied=tuple(
                rule.strip()
                for rule in str(plain.get("norm_rules_applied") or "").split(",")
                if rule.strip()
            ),
            original_currency=plain.get("original_currency"),
            original_amount=plain.get("original_amount"),
        ),
        account=account_ref(plain),
        money=money_from_row(plain),
        kind=str(plain.get("kind") or "unknown"),
        category=str(plain.get("category") or ""),
        inferred_category=str(plain.get("inferred_category") or "") or None,
        transaction_state=str(plain.get("txn_state") or "").upper() or "UNKNOWN",
        match_state=str(plain.get("match_state") or "") or "unknown",
        review_eligible=bool(plain.get("review_eligible", 1)),
        reason_codes=codes,
        projected=is_projected(plain),
        settled=is_settled(plain),
        statistics_eligible=is_statistics_eligible(plain),
        statistics_quarantined=is_statistics_quarantined(plain),
        uncategorized=bool(plain.get("is_uncategorized")),
        recurring=bool(plain.get("recurring_flag")),
        split=bool(plain.get("is_split")),
        reviewed=bool(plain.get("is_reviewed")),
        foreign_charge=bool(plain.get("is_foreign_charge")),
        provenance=Provenance(
            source=str(plain.get("source") or ""),
            transaction_version_id=plain.get("id"),
            run_id=plain.get("run_id"),
            source_hash=str(plain.get("source_hash") or ""),
            algorithm_version=str(plain.get("algorithm_version") or ""),
            ruleset_version=str(plain.get("ruleset_version") or ""),
        ),
    )


# --- the adapter-facing constructor -----------------------------------------


def parse_amount_value(value: Any, currency: str) -> Money:
    """Turn a source's amount — string, int or Decimal — into minor units."""
    try:
        return from_decimal(Decimal(str(value)), currency)
    except (InvalidOperation, OverflowError, ValueError, TypeError) as exc:
        raise EvidenceError(f"invalid amount {value!r} for {currency}") from exc


def build_record(
    *,
    transaction_id: str,
    posted_on: str,
    account: AccountRef,
    money: Money,
    payee_raw: str,
    category: str,
    account_names: set[str],
    exclusion_flag: bool | None,
    recurring_flag: bool,
    provider_payee: str | None = None,
    transacted_on: str | None = None,
    modified_at: str | None = None,
    inferred_category: str = "",
    excluded_from_f2s: bool = False,
    txn_state: str | None = None,
    match_state: str | None = None,
    scheduled_model_id: str | None = None,
    scheduled_due_on: str | None = None,
    is_split: bool = False,
    is_reviewed: bool = False,
) -> dict[str, Any]:
    """Assemble the normalized record. Both adapters go through here.

    Every key a downstream reader may see is written on this one path, so "the
    CSV and API records have the same shape" is a property of the code rather
    than of two lists somebody has to keep in step. A source that cannot supply
    a field passes nothing and gets the field's declared absent value — which
    is never a plausible-looking substitute drawn from a neighbouring field.
    """
    merchant = merchant_identity(payee_raw, provider_payee)
    semantics: Semantics = classify(
        category=category,
        payee_raw=payee_raw,
        amount_minor_units=money.minor_units,
        exclusion_flag=exclusion_flag,
        account_names=account_names,
    )
    record: dict[str, Any] = {
        "transaction_id": transaction_id,
        "posted_on": posted_on,
        "transacted_on": transacted_on,
        "modified_at": modified_at,
        # An unnamed account is recorded as unnamed. The provider's ID stays in
        # its own column, where the egress allowlist and the packet contract
        # both already refuse it by name.
        "account_name": account.name,
        "account_name_known": int(account.is_named),
        "account_id": account.provider_id,
        "amount_minor_units": money.minor_units,
        "currency": money.currency,
        "currency_exponent": money.exponent,
        "payee_raw": merchant.raw,
        "payee_normalized": merchant.normalized,
        "payee_canonical": merchant.canonical,
        "payee_display": merchant.safe_display(),
        "norm_rules_applied": ",".join(merchant.rules_applied),
        # The pre-conversion charge, when the descriptor carried one. This is
        # NOT the transaction's currency: the issuer already converted and
        # `money` is in the settlement currency. Identity evidence only.
        "original_currency": merchant.original_currency,
        "original_amount": merchant.original_amount,
        "is_foreign_charge": int(merchant.is_foreign_charge),
        "category": category,
        "inferred_category": inferred_category,
        "is_uncategorized": int(not category or category.strip().lower() == "uncategorized"),
        "exclusion_flag": 2 if exclusion_flag is None else int(bool(exclusion_flag)),
        "excluded_from_f2s": int(bool(excluded_from_f2s)),
        "recurring_flag": int(bool(recurring_flag)),
        "txn_state": txn_state or None,
        "match_state": match_state or None,
        "scheduled_model_id": scheduled_model_id or None,
        "scheduled_due_on": scheduled_due_on or None,
        "is_split": int(bool(is_split)),
        "is_reviewed": int(bool(is_reviewed)),
        "kind": semantics.kind.value,
        "poisons_statistics": int(semantics.poisons_statistics),
        "semantics_reasons": "; ".join(semantics.reasons),
    }
    return annotate_eligibility(record)


__all__ = [
    "UNKNOWN_ACCOUNT",
    "UNKNOWN_MERCHANT",
    "AccountRef",
    "EvidenceError",
    "MerchantIdentity",
    "Provenance",
    "TransactionEvidence",
    "account_ref",
    "build_record",
    "evidence_from_row",
    "merchant_identity",
    "parse_amount_value",
]
