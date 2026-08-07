"""Simplifi CSV export reader.

Verified schema (2026-08-04 export, 1,641 rows):

    Date, Account, Reviewed, Payee, Category, Attachments, Exclusion, Recurring, Amount

`Date` is human-readable ("Aug 3, 2026"). `Amount` has a leading space, a bare
minus for debits and no currency symbol. There is no transaction ID, no raw
statement descriptor, no currency and no split marker; callers must preserve
those source limitations rather than infer the missing fields.
"""

from __future__ import annotations

import csv
import hashlib
from collections import Counter
from datetime import datetime
from pathlib import Path

from ..money import parse_amount
from ..normalize import normalize
from ..semantics import annotate_eligibility, classify

EXPECTED_COLUMNS = [
    "Date",
    "Account",
    "Reviewed",
    "Payee",
    "Category",
    "Attachments",
    "Exclusion",
    "Recurring",
    "Amount",
]

DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y")


class SchemaError(ValueError):
    pass


def _parse_date(raw: str) -> str:
    text = (raw or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise SchemaError(f"unrecognised date format: {raw!r}")


def _yes(value: str) -> bool:
    return (value or "").strip().lower() in {"yes", "true", "1", "y"}


def _synthetic_id(posted_on: str, account: str, payee: str, amount: int, seq: int) -> str:
    """Content-addressed stand-in for the missing transaction ID.

    `seq` disambiguates genuine same-day duplicates (two identical $50 charges
    at one merchant), which do occur in this data.
    """
    key = f"{posted_on}|{account}|{payee}|{amount}|{seq}"
    return "csv_" + hashlib.sha256(key.encode()).hexdigest()[:24]


class SimplifiCsvSource:
    name = "csv"

    def __init__(self, path: Path):
        self.path = path

    def fetch(self) -> list[dict]:
        with self.path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in EXPECTED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise SchemaError(
                    f"export is missing expected columns {missing}; got {reader.fieldnames}"
                )
            rows = list(reader)

        account_names = {(r["Account"] or "").strip() for r in rows}
        account_names.discard("")

        seen: Counter[tuple] = Counter()
        records: list[dict] = []

        for row in rows:
            posted_on = _parse_date(row["Date"])
            account = (row["Account"] or "").strip()
            payee_raw = (row["Payee"] or "").strip()
            category = (row["Category"] or "").strip()
            money = parse_amount(row["Amount"])

            key = (posted_on, account, payee_raw, money.minor_units)
            seen[key] += 1

            desc = normalize(payee_raw)
            # Currency always comes from the account, never from the payee text.
            # A charge shown as "2.90 Euro ..." was already converted by the
            # issuer; the Amount column is USD. Treating EUR as the transaction
            # currency would make every sum across these rows wrong.
            currency = money.currency

            sem = classify(
                category=category,
                payee_raw=payee_raw,
                amount_minor_units=money.minor_units,
                exclusion_flag=_yes(row["Exclusion"]),
                account_names=account_names,
            )

            records.append(
                annotate_eligibility(
                    {
                        "transaction_id": _synthetic_id(
                            posted_on, account, payee_raw, money.minor_units, seen[key]
                        ),
                        "posted_on": posted_on,
                        "account_name": account,
                        "amount_minor_units": money.minor_units,
                        "currency": currency,
                        "currency_exponent": money.exponent,
                        "payee_raw": payee_raw,
                        "payee_normalized": desc.normalized,
                        "payee_canonical": desc.canonical,
                        "payee_display": desc.display,
                        "norm_rules_applied": ",".join(desc.rules_applied),
                        "original_currency": desc.original_currency,
                        "original_amount": desc.original_amount,
                        "is_foreign_charge": int(desc.original_currency is not None),
                        "category": category,
                        "is_uncategorized": int(category.lower() in {"", "uncategorized"}),
                        "exclusion_flag": int(_yes(row["Exclusion"])),
                        "recurring_flag": int(_yes(row["Recurring"])),
                        "kind": sem.kind.value,
                        "poisons_statistics": int(sem.poisons_statistics),
                        "semantics_reasons": "; ".join(sem.reasons),
                    }
                )
            )
        return records
