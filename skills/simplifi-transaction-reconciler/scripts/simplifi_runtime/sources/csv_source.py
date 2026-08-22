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

from ..evidence import AccountRef, build_record
from ..money import parse_amount

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

    #: The export carries no currency column, so the operator states it. USD is
    #: the default because that is what this dataset is, but a zero-decimal
    #: currency has to be *sayable*: parsing a JPY export as USD would multiply
    #: every amount by 100 and no downstream check would notice, because the
    #: figures stay internally consistent all the way to the report.
    def __init__(self, path: Path, currency: str = "USD"):
        self.path = path
        self.currency = (currency or "USD").upper()

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
            # Currency always comes from the export's declared currency, never
            # from the payee text. A charge shown as "2.90 Euro ..." was already
            # converted by the issuer; the Amount column is in the account's
            # currency. Treating EUR as the transaction currency would make
            # every sum across these rows wrong.
            money = parse_amount(row["Amount"], self.currency)

            key = (posted_on, account, payee_raw, money.minor_units)
            seen[key] += 1

            records.append(
                build_record(
                    transaction_id=_synthetic_id(
                        posted_on, account, payee_raw, money.minor_units, seen[key]
                    ),
                    posted_on=posted_on,
                    # The CSV has no account identifier at all, so there is no
                    # provider ID to carry and none is invented.
                    account=AccountRef(name=account),
                    money=money,
                    payee_raw=payee_raw,
                    category=category,
                    account_names=account_names,
                    exclusion_flag=_yes(row["Exclusion"]),
                    recurring_flag=_yes(row["Recurring"]),
                )
            )
        return records
