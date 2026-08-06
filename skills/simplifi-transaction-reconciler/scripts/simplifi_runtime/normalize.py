"""Descriptor normalization with a full transformation trace.

The pipeline keeps four distinct fields and never collapses them:

    raw        what the source gave us, untouched
    normalized human-readable, processor noise stripped
    canonical  lowercase slug, the merchant-memory key
    display    title-cased normalized, for the report

Every rule that fired is recorded in `rules_applied`, so a bad normalization is
debuggable instead of mysterious.

This is the highest-leverage code in the project and it is entirely offline.
Rules were written against the real 528-payee export, not invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

US_STATES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
]


@dataclass
class Descriptor:
    raw: str
    normalized: str
    canonical: str
    display: str
    rules_applied: list[str] = field(default_factory=list)
    # Some foreign card charges carry the pre-conversion amount in the payee
    # string, e.g. "2.90 Euro Tmb Bus Transit".
    #
    # THIS IS NOT THE TRANSACTION'S CURRENCY. The card issuer already converted;
    # the Amount column is USD. These two fields describe the *original charge*
    # and are informational only — never let them set `currency`.
    #
    # Their real value is merchant identity: stripping the prefix makes
    # "0.60 Euro Mercadona Calella" and "15.64 Euro Mercadona Calella" collapse
    # to one canonical merchant instead of two.
    original_currency: str | None = None
    original_amount: str | None = None


# --- individual rules -------------------------------------------------------
# Each returns (new_text, fired: bool). Order matters; see normalize().

_FOREIGN_PREFIX = re.compile(
    r"^\s*([\d]+(?:[.,]\d+)?)\s+(Euro|EUR|Pound|GBP|Yen|JPY|Franc|CHF|Krona|SEK)\s+",
    re.IGNORECASE,
)
_CURRENCY_NAMES = {
    "euro": "EUR",
    "eur": "EUR",
    "pound": "GBP",
    "gbp": "GBP",
    "yen": "JPY",
    "jpy": "JPY",
    "franc": "CHF",
    "chf": "CHF",
    "krona": "SEK",
    "sek": "SEK",
}

_CARD_TXN_ID = re.compile(r"\s*\(Card Transaction(?:\s+ID)?:\s*[^)]*\)\s*$", re.IGNORECASE)
_PROCESSOR_PREFIX = re.compile(r"^\s*(SQ|TST|SP|PY|PAYPAL|IC|WPY|EB)\s*\*\s*", re.IGNORECASE)
_PHONE = re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")
# Trailing ",<digits>,<3-digit code>" seen in card-network descriptors, e.g.
# "solano exp,montclair,nj,070420000,840". The final group looks like an ISO
# 4217 numeric code (840 = USD) but that is a hypothesis, not confirmed.
_NETWORK_TAIL = re.compile(r",\s*\d{6,},\s*\d{3}\s*$")
_TRAILING_STORE_NO = re.compile(r"[\s#]+\d{3,6}\s*$")
_LONG_DIGIT_RUN = re.compile(r"\b\d{7,}\b")
_TRUNCATION = re.compile(r"\s*(\.\.\.|\(cas)\s*$")
_MULTISPACE = re.compile(r"\s{2,}")


def _strip_state_suffix(text: str) -> tuple[str, bool]:
    stripped = text.rstrip(" .,")
    for sep in (",", " "):
        head, _, tail = stripped.rpartition(sep)
        if head and tail.upper() in US_STATES:
            return head.rstrip(" .,"), True
    return text, False


def normalize(raw: str) -> Descriptor:
    """Run the full pipeline. Never raises; worst case returns raw unchanged."""
    applied: list[str] = []
    text = raw or ""
    original_currency = None
    original_amount = None

    m = _FOREIGN_PREFIX.match(text)
    if m:
        original_amount = m.group(1)
        original_currency = _CURRENCY_NAMES.get(m.group(2).lower())
        text = text[m.end() :]
        applied.append("strip_original_charge_prefix")

    for name, pattern in (
        ("strip_card_txn_id", _CARD_TXN_ID),
        ("strip_processor_prefix", _PROCESSOR_PREFIX),
        ("strip_network_tail", _NETWORK_TAIL),
        ("strip_truncation_marker", _TRUNCATION),
        ("strip_phone", _PHONE),
        ("strip_long_digit_run", _LONG_DIGIT_RUN),
    ):
        new = pattern.sub(" ", text)
        if new != text:
            applied.append(name)
            text = new

    text, fired = _strip_state_suffix(text)
    if fired:
        applied.append("strip_state_suffix")

    new = _TRAILING_STORE_NO.sub("", text)
    if new != text and new.strip():
        applied.append("strip_trailing_store_number")
        text = new

    text = _MULTISPACE.sub(" ", text).strip(" .,-*")
    if not text:
        # Normalisation ate everything; fall back rather than emit an empty key.
        text = (raw or "").strip()
        applied.append("fallback_to_raw")

    canonical = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    display = text if text.isupper() or text.istitle() else text.title()

    return Descriptor(
        raw=raw,
        normalized=text,
        canonical=canonical or "unknown",
        display=display,
        rules_applied=applied,
        original_currency=original_currency,
        original_amount=original_amount,
    )
