"""Hierarchical merchant/category memory.

Two keys, most specific first:

    (account, canonical, currency, amount_band, sign)
    (canonical, currency, sign)

The account component is the account's correlation key, not its display name:
an account with no name is its own account, and keying on the empty display
would pool every unnamed account in the dataset into one memory.

Currency is part of both keys because the bands are minor-unit thresholds. In a
mixed-currency dataset ¥1,500 and $15.00 are the same integer, and a key that
omitted the currency would teach one merchant's categories from the other's.

A level matches only with `n >= MIN_OBSERVATIONS` and `>= MIN_PURITY` agreement.
Below that the level is *ambiguous*, not wrong: we fall through to the next
level, and exhausting all levels means "I don't know" — which routes the
transaction to a model rather than guessing.

Rows that `poisons_statistics` (transfers, card payments, balance adjustments,
investments) never train the memory. Learning from them is what makes a
classifier decide a bank name is a spending category.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from .evidence import account_ref
from .semantics import is_statistics_eligible

MIN_OBSERVATIONS = 3
MIN_PURITY = 0.9

#: Amount bands in minor units, used only at the most specific level. Chosen to
#: separate "subscription" from "weekly shop" from "big purchase".
BANDS = ((0, 2_000), (2_000, 10_000), (10_000, 50_000), (50_000, None))


def amount_band(minor_units: int) -> str:
    v = abs(minor_units)
    for lo, hi in BANDS:
        if hi is None or v < hi:
            return f"{lo}-{hi if hi is not None else 'inf'}"
    return "unknown"


def sign_of(minor_units: int) -> str:
    return "credit" if minor_units > 0 else "debit"


@dataclass
class Proposal:
    category: str
    confidence: float
    level: str
    observations: int
    purity: float

    @property
    def evidence(self) -> dict:
        return {
            "level": self.level,
            "observations": self.observations,
            "purity": round(self.purity, 3),
        }


class MerchantMemory:
    """Built from categorised, non-poisoning history."""

    def __init__(self) -> None:
        self._levels: list[tuple[str, dict[tuple, Counter]]] = [
            ("account+merchant+currency+band+sign", defaultdict(Counter)),
            ("merchant+currency+sign", defaultdict(Counter)),
        ]

    @staticmethod
    def _keys(row: dict) -> list[tuple]:
        canon = row["payee_canonical"]
        sign = sign_of(row["amount_minor_units"])
        currency = str(row.get("currency") or "USD").upper()
        account = account_ref(row).correlation_key
        return [
            (account, canon, currency, amount_band(row["amount_minor_units"]), sign),
            (canon, currency, sign),
        ]

    def train(self, rows: list[dict]) -> None:
        for row in rows:
            if not is_statistics_eligible(row) or row["is_uncategorized"]:
                continue
            category = (row["category"] or "").strip()
            if not category:
                continue
            for (_, table), key in zip(self._levels, self._keys(row), strict=True):
                table[key][category] += 1

    def propose(self, row: dict) -> Proposal | None:
        for (name, table), key in zip(self._levels, self._keys(row), strict=True):
            counts = table.get(key)
            if not counts:
                continue
            total = sum(counts.values())
            category, top = counts.most_common(1)[0]
            if total < MIN_OBSERVATIONS:
                continue
            purity = top / total
            if purity < MIN_PURITY:
                # Ambiguous at this level — fall through rather than guess.
                continue
            return Proposal(
                category=category,
                confidence=round(purity * min(1.0, total / 10), 3),
                level=name,
                observations=total,
                purity=purity,
            )
        return None

    def stats(self) -> dict:
        _name, table = self._levels[-1]
        confident = ambiguous = 0
        for counts in table.values():
            total = sum(counts.values())
            if total < MIN_OBSERVATIONS:
                continue
            if counts.most_common(1)[0][1] / total >= MIN_PURITY:
                confident += 1
            else:
                ambiguous += 1
        return {"confident_merchants": confident, "ambiguous_merchants": ambiguous}
