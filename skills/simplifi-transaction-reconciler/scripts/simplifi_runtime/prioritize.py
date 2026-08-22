"""Evidence-bearing review prioritization.

Deliberately NOT called fraud detection. This surfaces what deserves a look;
the data cannot support stronger claims than that.

Statistics: merchant spending is heavy-tailed with small n, so 3-sigma is wrong.
We use median + MAD-based robust z, plus a ratio to median, and require n >= 5
at whatever baseline level we fall back to.

Every signal carries its own evidence. A bare flag is not actionable.

Rows that `poisons_statistics` are excluded from baselines AND from scoring —
a $4,000 card payment in the same series as $40 coffees destroys the statistics.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise

from .evidence import account_ref, evidence_from_row
from .money import money_from_row
from .semantics import is_settled, is_statistics_eligible

MIN_BASELINE_N = 5
ROBUST_Z_THRESHOLD = 4.0
RATIO_THRESHOLD = 4.0
MAD_SCALE = 1.4826
DUPLICATE_WINDOW_DAYS = 3
SUBSCRIPTION_CREEP_PCT = 0.10
#: An exact amount repeated this often at one merchant is a standing fare or
#: subscription (commuter rail, coffee), not a double charge.
REPEAT_AMOUNT_IS_ROUTINE = 3
MIN_SERIES_FOR_CREEP = 4
#: If more than this share of a merchant's own history would trip the outlier
#: test, the merchant is inherently variable and the test means nothing there.
#: Amazon's median is ~$19 because of many small orders, so every ordinary
#: larger purchase scored 5.0 and dominated the list.
MAX_MERCHANT_FIRE_RATE = 0.10
# A single unusual charge should not make a short merchant history ineligible
# for outlier detection. Wait until there is enough history for a fire-rate
# estimate to mean more than one observation.
MIN_VARIABLE_HISTORY = 10
#: Below this, a same-day repeat is routine (two coffees, two small Amazon
#: items, two subway fares) and not worth a line in a review list.
DUPLICATE_MIN_MINOR_UNITS = 2_000


@dataclass
class Signal:
    name: str
    score: float
    evidence: dict


@dataclass
class Prioritized:
    row: dict
    signals: list[Signal] = field(default_factory=list)

    @property
    def total_score(self) -> float:
        return round(sum(s.score for s in self.signals), 2)


def _robust_stats(values: list[int]) -> tuple[float, float]:
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values]) * MAD_SCALE
    return med, mad


def _d(iso: str) -> date:
    return date.fromisoformat(iso)


def _major_units(row: dict, minor_units: int | float) -> float:
    """A derived figure in the row's own currency, not in assumed cents.

    Medians and baselines are computed in minor units and reported in major
    ones. The conversion is the row's, so it goes through the row's money.
    """
    return money_from_row(row, minor_units=round(minor_units)).as_float


def analyse(rows: list[dict], today: date | None = None) -> list[Prioritized]:
    """Score every non-poisoning row. Returns only rows with at least one signal."""
    today = today or date.today()
    scored = [r for r in rows if is_statistics_eligible(r)]

    # --- baselines, most specific first ------------------------------------
    by_merchant: dict[str, list[int]] = defaultdict(list)
    baseline_rows = [
        r for r in scored if r["kind"] in {"spend", "fee"} and r["amount_minor_units"] < 0
    ]
    for r in baseline_rows:
        amt = abs(r["amount_minor_units"])
        if amt == 0:
            continue
        by_merchant[r["payee_canonical"]].append(amt)

    first_seen: dict[str, date] = {}
    merchant_dates: dict[str, list[date]] = defaultdict(list)
    for r in sorted(scored, key=lambda x: x["posted_on"]):
        canon = r["payee_canonical"]
        first_seen.setdefault(canon, _d(r["posted_on"]))
        merchant_dates[canon].append(_d(r["posted_on"]))

    # Merchants whose own spread makes the outlier test meaningless.
    variable_merchants: set[str] = set()
    for canon, amounts in by_merchant.items():
        if len(amounts) < MIN_VARIABLE_HISTORY:
            continue
        med, mad = _robust_stats(amounts)
        if med <= 0:
            continue
        fires = sum(
            1
            for v in amounts
            if v > med
            and (
                v / med >= RATIO_THRESHOLD or (mad > 0 and abs(v - med) / mad >= ROBUST_Z_THRESHOLD)
            )
        )
        if fires / len(amounts) > MAX_MERCHANT_FIRE_RATE:
            variable_merchants.add(canon)

    results: dict[str, Prioritized] = {}

    def add(row: dict, signal: Signal) -> None:
        p = results.setdefault(row["transaction_id"], Prioritized(row=row))
        p.signals.append(signal)

    # --- amount_outlier -----------------------------------------------------
    for r in scored:
        amt = abs(r["amount_minor_units"])
        # Only outbound spending can be an outlier. Credits and refunds
        # exceeding the typical purchase size is normal, not notable.
        if amt == 0 or r["kind"] != "spend":
            continue
        if r["payee_canonical"] in variable_merchants:
            continue
        # MERCHANT BASELINE ONLY. Category and global baselines were tried and
        # removed: "this Shopping purchase is 4x the median Shopping purchase"
        # is not a finding, because that category spans $5 to $500 legitimately.
        # It produced 122 flags (7% of all rows) and buried the real ones.
        # Without merchant history we simply cannot call an amount unusual.
        for level, pool in (("merchant", by_merchant[r["payee_canonical"]]),):
            peers = [v for v in pool if v != amt] or pool
            if len(peers) < MIN_BASELINE_N:
                continue
            med, mad = _robust_stats(peers)
            if med <= 0:
                break
            ratio = amt / med
            z = abs(amt - med) / mad if mad > 0 else 0.0
            if (z >= ROBUST_Z_THRESHOLD or ratio >= RATIO_THRESHOLD) and amt > med:
                add(
                    r,
                    Signal(
                        "amount_outlier",
                        min(5.0, ratio),
                        {
                            "baseline_level": level,
                            "observations": len(peers),
                            "median": _major_units(r, med),
                            "median_minor_units": med,
                            "amount": _major_units(r, amt),
                            "amount_minor_units": amt,
                            "ratio": round(ratio, 2),
                            "robust_z": round(z, 2),
                        },
                    ),
                )
            break  # only the most specific level with enough data

    # --- duplicate ----------------------------------------------------------
    amount_freq = Counter((r["payee_canonical"], r["amount_minor_units"]) for r in scored)
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for r in scored:
        buckets[
            (r["payee_canonical"], r["amount_minor_units"], account_ref(r).correlation_key)
        ].append(r)
    for (canon, amt_key, _acct), group in buckets.items():
        if len(group) < 2:
            continue
        # A fixed fare charged repeatedly all year (commuter rail, a daily
        # coffee) is not a double charge. Only flag amounts that are unusual
        # for this merchant in the first place.
        if amount_freq[(canon, amt_key)] > REPEAT_AMOUNT_IS_ROUTINE:
            continue
        group.sort(key=lambda x: x["posted_on"])
        for a, b in pairwise(group):
            gap = (_d(b["posted_on"]) - _d(a["posted_on"])).days
            if (
                gap <= DUPLICATE_WINDOW_DAYS
                and abs(b["amount_minor_units"]) >= DUPLICATE_MIN_MINOR_UNITS
            ):
                # Score with size: a repeated $700 airfare matters, a repeated
                # $2.90 subway fare does not.
                score = min(5.0, 2.0 + abs(b["amount_minor_units"]) / 20_000)
                add(
                    b,
                    Signal(
                        "duplicate",
                        round(score, 2),
                        {
                            "days_apart": gap,
                            "amount": _major_units(b, abs(b["amount_minor_units"])),
                            "amount_minor_units": abs(b["amount_minor_units"]),
                            "first_seen_on": a["posted_on"],
                            "account": account_ref(b).display,
                        },
                    ),
                )

    # --- subscription_creep -------------------------------------------------
    # The CSV's Recurring flag is set on only 8 of 1,641 rows, so cadence is
    # inferred from date spacing instead: >=4 charges, roughly monthly. Keep
    # accounts separate: two monthly charges for the same provider must not be
    # interleaved into a false twice-monthly series.
    series_by_account: dict[tuple[str, tuple[str, str] | None], list[dict]] = defaultdict(list)
    for r in scored:
        if r["kind"] in {"spend", "fee"} and r["amount_minor_units"] < 0 and r["payee_canonical"]:
            series_by_account[(r["payee_canonical"], account_ref(r).correlation_key)].append(r)

    for (_canon, _identity), series_rows in series_by_account.items():
        series = sorted(
            series_rows,
            key=lambda x: x["posted_on"],
        )
        dates = [_d(r["posted_on"]) for r in series]
        if len(dates) < 4:
            continue
        gaps = [(b - a).days for a, b in pairwise(sorted(dates))]
        if not gaps or not (26 <= statistics.median(gaps) <= 34):
            continue
        amounts = [abs(r["amount_minor_units"]) for r in series]
        for i in range(1, len(series)):
            prior = amounts[:i]
            if len(prior) < MIN_SERIES_FOR_CREEP:
                continue
            expected = statistics.median(prior)
            if expected > 0 and amounts[i] > expected * (1 + SUBSCRIPTION_CREEP_PCT):
                add(
                    series[i],
                    Signal(
                        "subscription_creep",
                        2.0,
                        {
                            "merchant": evidence_from_row(series[i]).merchant.safe_display(),
                            "previous_typical": _major_units(series[i], expected),
                            "previous_typical_minor_units": expected,
                            "now": _major_units(series[i], amounts[i]),
                            "now_minor_units": amounts[i],
                            "increase_pct": round(100 * (amounts[i] / expected - 1), 1),
                            "observations": len(prior),
                            "median_gap_days": statistics.median(gaps),
                        },
                    ),
                )
                break  # report the first increase per series, not every one

    # --- refund_without_original -------------------------------------------
    for r in scored:
        if r["kind"] != "refund":
            continue
        amt = abs(r["amount_minor_units"])
        d = _d(r["posted_on"])
        refund_account = account_ref(r).correlation_key
        match = any(
            o["payee_canonical"] == r["payee_canonical"]
            and abs(o["amount_minor_units"]) == amt
            and o["amount_minor_units"] < 0
            and 0 <= (d - _d(o["posted_on"])).days <= 120
            and refund_account is not None
            and account_ref(o).correlation_key == refund_account
            for o in scored
        )
        if not match:
            add(
                r,
                Signal(
                    "refund_without_original",
                    1.5,
                    {
                        "merchant": evidence_from_row(r).merchant.safe_display(),
                        "amount": _major_units(r, amt),
                        "amount_minor_units": amt,
                        "note": "no matching debit within 120 days",
                    },
                ),
            )

    # --- new_merchant: CONTEXT ONLY -----------------------------------------
    # Attached to rows already flagged for another reason. On its own, "you
    # visited a restaurant once" is not worth a line in a review list — it
    # produced 90 entries and drowned the real signals.
    window_start = today - timedelta(days=60)
    for r in scored:
        if r["transaction_id"] not in results:
            continue
        canon = r["payee_canonical"]
        d = _d(r["posted_on"])
        if d >= window_start and first_seen[canon] == d and len(merchant_dates[canon]) == 1:
            add(
                r,
                Signal("new_merchant", 0.0, {"note": "first and only appearance in this dataset"}),
            )

    return sorted(results.values(), key=lambda p: -p.total_score)


def activity_staleness(rows: list[dict], today: date | None = None) -> list[dict]:
    """Flag accounts whose observed transaction activity is stale.

    This is not connection health; use the API probe for provider refresh
    status and institution errors.
    """
    today = today or date.today()
    last: dict[str, date] = {}
    for r in rows:
        if not is_settled(r) or r["posted_on"] > today.isoformat():
            continue
        d = _d(r["posted_on"])
        name = account_ref(r).display
        if name not in last or d > last[name]:
            last[name] = d
    out = []
    for name, d in sorted(last.items(), key=lambda kv: kv[1]):
        days = (today - d).days
        out.append(
            {
                "account": name,
                "last_transaction": d.isoformat(),
                "days_stale": days,
                "status": "stale" if days > 14 else "ok",
            }
        )
    return out
