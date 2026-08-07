"""Money handling. Integer minor units only — never floats.

Store amount_minor_units + currency + currency_exponent, so that
JPY (exponent 0) and USD (exponent 2) are both representable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# ISO 4217 exponents for currencies we might plausibly see. Default is 2.
CURRENCY_EXPONENTS = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "CHF": 2,
    "SEK": 2,
    "JPY": 0,
    "KRW": 0,
    "CLP": 0,
}

_AMOUNT_CLEAN = re.compile(r"[,$\s]")


@dataclass(frozen=True)
class Money:
    minor_units: int
    currency: str = "USD"

    @property
    def exponent(self) -> int:
        return CURRENCY_EXPONENTS.get(self.currency, 2)

    @property
    def as_decimal(self) -> Decimal:
        return Decimal(self.minor_units) / (10**self.exponent)

    def __str__(self) -> str:
        return f"{self.as_decimal:.{self.exponent}f} {self.currency}"


def parse_amount(raw: str | None, currency: str = "USD") -> Money:
    """Parse a Simplifi CSV amount into minor units.

    The export writes amounts with a leading space, a bare minus for debits,
    and no currency symbol — e.g. ' -45.84'. Commas appear in larger values.

    Raises ValueError on anything unparseable rather than silently zeroing.
    """
    if raw is None:
        raise ValueError("amount is None")
    cleaned = _AMOUNT_CLEAN.sub("", raw)
    if not cleaned or cleaned in {"-", "+"}:
        raise ValueError(f"unparseable amount: {raw!r}")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount: {raw!r}") from exc
    if not value.is_finite():
        raise ValueError(f"unparseable amount: {raw!r}")

    exponent = CURRENCY_EXPONENTS.get(currency, 2)
    scaled = value * (10**exponent)
    # Amounts are always exact to the currency's precision in this data; if a
    # fractional minor unit ever appears we want to know rather than round.
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount {raw!r} has sub-minor-unit precision for {currency}")
    return Money(int(scaled), currency)
