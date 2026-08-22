"""Money handling. Integer minor units only — never floats.

Store amount_minor_units + currency + currency_exponent, so that
JPY (exponent 0) and USD (exponent 2) are both representable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# ISO 4217 minor-unit exponents. Complete for every currency whose exponent is
# NOT 2, which is what makes the default safe: anything absent here really does
# have two decimal places, rather than merely not having been thought of.
#
# It was a sample of eight before, and that was survivable only while the
# currency was hard-coded. Once `ingest --currency` let an operator name one,
# an absent BHD (three places) would have rejected a valid 1.234 outright and
# stored 1.23 as 123 minor units — BHD 0.123, wrong by a factor of ten, in a
# figure nothing downstream could recheck.
#
# Source: ISO 4217 Table A.1. Historical and fund codes are included where they
# share the non-default exponents; a code not listed is exponent 2 by the
# standard's own default.
CURRENCY_EXPONENTS = {
    # Zero decimal places — the amount IS the minor unit.
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "PYG": 0,
    "RWF": 0,
    "UGX": 0,
    "UYI": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
    # Three decimal places.
    "BHD": 3,
    "IQD": 3,
    "JOD": 3,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "TND": 3,
    # Four decimal places.
    "CLF": 4,
    "UYW": 4,
}

#: The default for anything unlisted, which by ISO 4217 is every remaining
#: currency. Named rather than inlined so the two places that rely on it say so.
DEFAULT_EXPONENT = 2

_CURRENCY_CODE = re.compile(r"^[A-Za-z]{3}$")

_AMOUNT_CLEAN = re.compile(r"[,$\s]")


@dataclass(frozen=True)
class Money:
    minor_units: int
    currency: str = "USD"

    @property
    def exponent(self) -> int:
        return exponent_for(self.currency)

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
    """The ISO 4217 minor-unit exponent.

    Unlisted means exponent 2, and that is now a statement rather than a guess:
    `CURRENCY_EXPONENTS` carries every currency whose exponent differs from the
    standard's default.

    Deliberately lenient, because this also reads rows back out of the
    database. A stored row naming a currency this table has never heard of must
    still be readable; refusing here would make an old row unreadable rather
    than merely uncertain. Input is where a currency is refused — see
    :func:`parse_currency`.
    """
    return CURRENCY_EXPONENTS.get((currency or "USD").upper(), DEFAULT_EXPONENT)


def parse_currency(raw: str | None) -> str:
    """Validate and normalize a currency an operator typed.

    Refuses anything that is not a three-letter code. `--currency dollars` and
    `--currency USDD` are typos, and the cost of accepting one is an entire
    dataset scaled by the wrong power of ten with every figure internally
    consistent — there is nothing downstream that could notice.
    """
    text = (raw or "").strip()
    if not _CURRENCY_CODE.match(text):
        raise ValueError(
            f"{raw!r} is not an ISO 4217 currency code; give three letters, e.g. USD or JPY"
        )
    return text.upper()


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
