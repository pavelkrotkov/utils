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

    def formatted(self, *, grouped: bool = False) -> str:
        """The amount alone, at this currency's precision.

        Callers that render money used to divide by 100 inline. That is right
        for USD and silently wrong for JPY, where 1000 minor units is ¥1000 and
        not ¥10.00 — an error of two orders of magnitude in a figure a person is
        being asked to act on.
        """
        spec = f",.{self.exponent}f" if grouped else f".{self.exponent}f"
        return format(self.as_decimal, spec)

    @property
    def as_float(self) -> float:
        """Major units as a float, for evidence dictionaries and JSON.

        Lossy by construction; never round-trip a stored amount through this.
        `minor_units` remains the authority and travels alongside it.
        """
        return float(self.as_decimal)


def exponent_for(currency: str | None) -> int:
    """The ISO 4217 minor-unit exponent, defaulting to 2 for anything unlisted."""
    return CURRENCY_EXPONENTS.get((currency or "USD").upper(), 2)


def from_decimal(value: Decimal, currency: str = "USD") -> Money:
    """Scale a major-unit decimal into minor units for `currency`.

    Raises ValueError rather than rounding: an amount with more precision than
    the currency has is a mapping bug or a currency mismatch, and rounding it
    would hide which.
    """
    if not value.is_finite():
        raise ValueError(f"amount {value!r} is not finite")
    exponent = exponent_for(currency)
    scaled = value * (10**exponent)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount {value!r} has sub-minor-unit precision for {currency}")
    return Money(int(scaled), (currency or "USD").upper())


def money_from_row(row, *, minor_units: int | None = None) -> Money:
    """Rebuild a `Money` from a stored or normalized transaction record.

    Downstream code reads rows, not adapters, so this is the one place that
    knows a row's amount columns. `minor_units` overrides the row's own amount
    for derived figures — a median, a baseline — which share the row's currency
    but not its value.
    """
    currency = str(row.get("currency") or "USD").upper()
    amount = row.get("amount_minor_units") if minor_units is None else minor_units
    return Money(int(amount or 0), currency)


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

    # Amounts are always exact to the currency's precision in this data; if a
    # fractional minor unit ever appears we want to know rather than round.
    try:
        return from_decimal(value, currency)
    except ValueError as exc:
        raise ValueError(f"unparseable amount {raw!r}: {exc}") from exc
