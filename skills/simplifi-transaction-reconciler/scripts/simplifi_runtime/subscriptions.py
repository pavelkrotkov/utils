"""Sanity checks on recurring charges.

Detect recurring charges from CLEARED activity only. A provider's recurring
model is an input to check, not a source of truth.

Checks include projected-but-uncleared (ghost), post-schedule billing (zombie),
material price changes (hike), concurrent candidate series (twin), and regular
series that stopped (lapsed or possible successor).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from itertools import pairwise
from typing import Any

from .evidence import account_ref
from .money import Money, money_from_row
from .semantics import is_projected, is_statistics_eligible

#: The finding kinds this module can produce. A stable vocabulary, because it
#: is a reason code in the packet and a chip in the report: consumers switch on
#: it, and a kind invented at a call site would reach them unannounced.
FINDING_KINDS = ("zombie", "hike", "twin", "renamed", "ghost", "lapsed")

#: A series needs this many cleared charges before we will call it recurring.
#: Two is coincidence; three is a pattern.
MIN_CHARGES = 3

#: Monthly-ish. Real billing dates wander (weekends, month lengths), so this is
#: deliberately loose — the alternative is missing a subscription that bills on
#: the 28th and lands on the 1st.
MIN_DAYS, MAX_DAYS = 20, 45

#: Report a price change above this. 15% is above card-network FX noise and
#: below the "they raised it and hoped nobody noticed" threshold.
HIKE_RATIO = 1.15

#: Two intervals of silence before a series counts as stopped.
SILENT_INTERVALS = 2.0

#: A SUBSCRIPTION charges the same amount every time. A utility bill, a grocery
#: run and a sushi habit do not. Coefficient of variation (stdev/mean) separates
#: them, and it is the single most useful filter here.
#:
#: Without this filter, variable bills and ordinary purchases can dominate the
#: report with technically-derived but practically-useless findings.
MAX_VARIATION = 0.12

#: How many recent charges define "the current price". Four covers a quarter of
#: monthly billing — long enough to be stable, short enough that a price change
#: two years ago does not disqualify a live subscription.
RECENT_WINDOW = 4

# A single latest charge is evidence of a change, not evidence of a new price
# regime. Require two observations on each side of the change before reporting
# a stable hike and keep those windows disjoint.
PRICE_REGIME_OBSERVATIONS = 2

#: A subscription is something you SIGNED UP FOR and can cancel. A mortgage,
#: insurance premium or tax payment is a fixed recurring obligation — it passes
#: every statistical test here and belongs in none of these checks. Listing a
#: $10,542 mortgage above a $6.50 Tidal bill also drowns the report.
BILL_CATEGORIES = {
    "mortgage",
    "mortgage interest",
    "mortgage principal",
    "rent",
    "hoa dues",
    "property tax",
    "personal property tax",
    "vehicle property tax",
    "federal tax",
    "state tax",
    "local tax",
    "sales tax",
    "medicare",
    "sdi",
    "car insurance",
    "home insurance",
    "health insurance",
    "life insurance",
    "loan insurance",
    "loan payment",
    "loan principal",
    "loan interest",
    "student loan",
    "child support",
    "alimony",
    "gas & electric",
    "water",
    "trash",
    "internet & cable",
    "utilities",
}

#: Ghost series with NO cleared charge at all are bill reminders the user set up
#: by hand. Expected, not findings. Only report a ghost that used to be real.
MIN_GHOST_HISTORY = 1


def _text(money: Money) -> str:
    """A money value rendered for a sentence, at its own currency's precision.

    Derived figures used to be carried around as major-unit floats and rendered
    by the caller, which read `$1,000.00 -> $1,200.00` whatever the currency: a
    ¥1,000 to ¥1,200 increase was reported in dollars — a number a reader would
    act on, with a symbol that was simply wrong.
    """
    return f"{money.formatted(grouped=True)} {money.currency}"


@dataclass(frozen=True)
class SeriesRef:
    """One merchant series, as everything outside this module may see it.

    Deliberately not the `Series` itself and deliberately not its dictionary
    key. The key is `f"{merchant}::{account correlation}"`, and that
    correlation is built from the provider's account ID — a join key that
    separates two people billed by the same merchant. It is not evidence, and a
    key is exactly the sort of string that ends up in an artifact once anything
    outside this module can reach it.
    """

    merchant: str
    account: str
    transaction_ids: tuple[str, ...]
    monthly: Money
    interval_days: float
    last_charge: str | None

    @property
    def label(self) -> str:
        """A human-readable name that never carries the account key."""
        return f"{self.merchant} [{self.account}]" if self.account else self.merchant


@dataclass(frozen=True)
class RecurringFinding:
    """One recurring-analysis result, complete enough to render from.

    The packet and the report both build their output from this and nothing
    else. They used to re-derive: the report parsed nothing but read a float
    and formatted it, the packet guessed the finding's currency by looking up a
    member transaction, and the meaning of a finding lived in `detail` — a
    sentence — so any consumer wanting a number had to either re-run the
    analysis or read English.

    `detail` is still here, because a rendered sentence is the right thing to
    show a human. It is now a rendering *of* the structure rather than the only
    place the structure exists.
    """

    kind: str
    series: tuple[SeriesRef, ...]
    detail: str
    annual_impact: Money
    #: Named money facts specific to the kind — `previous`/`current` for a
    #: hike, `projected_charge` for a ghost. Money, never a bare number: a
    #: figure whose currency a reader has to assume is a figure that is wrong
    #: in every currency but one.
    amounts: Mapping[str, Money] = field(default_factory=dict)
    #: Named non-money facts — counts, day gaps, dates, the shared token.
    facts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def merchant(self) -> str:
        """The display name, joined across series for multi-series kinds."""
        return " + ".join(ref.label for ref in self.series)

    @property
    def transaction_ids(self) -> tuple[str, ...]:
        """Every contributing transaction, across every series in the finding."""
        return tuple(sorted({txid for ref in self.series for txid in ref.transaction_ids}))


@dataclass
class _Draft:
    """A finding under construction, still holding its internal series keys.

    The keys are needed for the successor pass, which has to look a lapsed
    series back up and edit its detail. Keeping them on a private type — rather
    than on the published one with a note asking consumers not to read it —
    means the key cannot leave this module at all.
    """

    kind: str
    keys: tuple[str, ...]
    detail: str
    annual_impact: Money
    amounts: dict[str, Money] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    #: Overrides the published series cost where the series median would
    #: misdescribe it. A hike is the case: the median spans both price regimes,
    #: so a finding whose `amounts.current` says 20.00 would publish a monthly
    #: cost of about 9.82 beside it, and a consumer reading the structure
    #: rather than the sentence would understate a live subscription.
    series_monthly: Money | None = None


@dataclass
class Series:
    merchant: str
    identity: str = ""
    charges: list[dict] = field(default_factory=list)
    projected: list[dict] = field(default_factory=list)

    @property
    def label(self) -> str:
        """A human-readable series name that never carries the account key.

        `identity` is a join key and may be the provider's account ID. It
        separates two people billed by the same merchant; it is not evidence,
        and a label is exactly the sort of string that ends up in an artifact.
        """
        account = self.account_display
        return f"{self.merchant} [{account}]" if account else self.merchant

    @property
    def account_display(self) -> str:
        """The safe account name shared by this series, or nothing."""
        for row in (*self.charges, *self.projected):
            ref = account_ref(row)
            if ref.is_named:
                return ref.name
        return ""

    @property
    def ref(self) -> SeriesRef:
        """The safe, publishable view of this series.

        Everything a consumer is allowed to know: the normalized merchant, the
        account's display name where it has one, the member transaction IDs and
        the cadence. Not `identity` — that is a join key built from the
        provider's account ID, and it exists to keep two people billed by the
        same merchant apart, not to be read.
        """
        return SeriesRef(
            merchant=self.merchant,
            account=self.account_display,
            transaction_ids=self.transaction_ids,
            monthly=self.monthly if self.charges else Money(0, self.currency),
            interval_days=round(self.interval_days, 1),
            last_charge=self.last_charge.isoformat() if self.last_charge else None,
        )

    @property
    def transaction_ids(self) -> tuple[str, ...]:
        """Stable member IDs without exposing the internal account identity."""
        return tuple(
            sorted(
                {
                    str(row["transaction_id"])
                    for row in [*self.charges, *self.projected]
                    if row.get("transaction_id")
                }
            )
        )

    @property
    def currency(self) -> str:
        """The series' currency, taken from its own rows.

        A series is per-merchant and per-account, so its charges share an
        account and therefore a currency. Reported so that every figure derived
        from `amounts` can be rendered as money rather than as a bare number
        that a reader has to assume is dollars.
        """
        for row in (*self.charges, *self.projected):
            code = str(row.get("currency") or "").strip()
            if code:
                return code.upper()
        return "USD"

    def money(self, minor_units: float) -> Money:
        """A derived figure carried back into this series' own currency.

        Takes minor units, because every figure derived here is derived from
        minor units. The old signature took major units and scaled by the
        currency's exponent, which meant each caller had to divide first and
        this had to multiply back — two lossy steps around arithmetic that was
        exact to begin with.
        """
        return Money(round(minor_units), self.currency)

    @property
    def amounts(self) -> list[int]:
        """Charge magnitudes in minor units, which is what they are stored as.

        Integers, not major-unit floats. Every statistic below — median, stdev,
        the hike ratio — is computed on these, so the only rounding in the
        module happens once, where a derived figure becomes a `Money`.
        """
        return [abs(money_from_row(c).minor_units) for c in self.charges]

    @property
    def dates(self) -> list[date]:
        return [date.fromisoformat(c["posted_on"]) for c in self.charges]

    @property
    def interval_days(self) -> float:
        d = sorted(self.dates)
        gaps = [(b - a).days for a, b in pairwise(d)]
        return statistics.median(gaps) if gaps else 0.0

    @property
    def last_charge(self) -> date | None:
        """None when a series is PURE projection — never a real charge.

        This is not a corner case: a subscription cancelled before its first
        clearing, or one Simplifi invented, has projections and no charges. The
        first version returned max() of an empty list and crashed on real data.
        """
        return max(self.dates) if self.charges else None

    @property
    def monthly_minor(self) -> float:
        """Normalised to a month, so annual plans compare with monthly ones."""
        interval = self.interval_days or 30
        return statistics.median(self.amounts) * (30.44 / interval)

    @property
    def monthly(self) -> Money:
        """The monthly cost as money, in this series' own currency."""
        return self.money(self.monthly_minor)

    @property
    def annual(self) -> Money:
        """The yearly cost as money. Rounded once, from the monthly figure."""
        return self.money(self.monthly_minor * 12)

    @property
    def recent_amounts(self) -> list[int]:
        """The last few charges, oldest first."""
        return [a for _, a in sorted(zip(self.dates, self.amounts, strict=True))][-RECENT_WINDOW:]

    @property
    def variation(self) -> float:
        """stdev/mean over the RECENT window, not all history.

        Measuring across all history punished subscriptions for having a price
        change, which is backwards — a price change is the thing worth
        reporting, not a reason to stop watching. Boxcar (0.147) and SimpliSafe
        (0.175) both fell just outside a 0.12 threshold because each raised its
        price once; on their last four charges both are rock steady.

        Full-history spread still gets reported, as `hike`.
        """
        a = self.recent_amounts
        if len(a) < 2 or statistics.mean(a) == 0:
            return 0.0
        return statistics.stdev(a) / statistics.mean(a)


def _series(rows: list[dict]) -> dict[str, Series]:
    """Group into per-account merchant series.

    The account identity prevents two people or accounts billed by the same
    provider from being interleaved into one cadence. Rows without account
    metadata retain the historical merchant-only key for fixture and CSV
    compatibility.
    """
    out: dict[str, Series] = defaultdict(lambda: Series(""))
    for r in rows:
        if not is_statistics_eligible(r, allow_projected=True):
            continue
        if r.get("kind") not in {"spend", "fee"}:
            continue
        if r["amount_minor_units"] >= 0:  # refunds are not subscriptions
            continue
        key = r.get("payee_canonical") or ""
        if not key:
            continue
        leaf = (r.get("category") or "").split(":")[-1].strip().lower()
        if leaf in BILL_CATEGORIES:
            continue
        correlation = account_ref(r).correlation_key
        identity = "" if correlation is None else ":".join(correlation)
        series_key = f"{key}::{identity}" if identity else key
        s = out[series_key]
        s.merchant = key
        s.identity = identity
        (s.projected if is_projected(r) else s.charges).append(r)
    return out


def _is_recurring(s: Series) -> bool:
    """Fixed amount, regular cadence, seen enough times.

    All three conditions matter. Dropping the amount check admits mortgages and
    grocery runs; dropping the cadence check admits anything bought twice.
    """
    return _has_regular_cadence(s) and s.variation <= MAX_VARIATION


def _has_regular_cadence(s: Series) -> bool:
    return len(s.charges) >= MIN_CHARGES and MIN_DAYS <= s.interval_days <= MAX_DAYS


def _hike(key: str, s: Series, interval: float) -> _Draft | None:
    """Return a price-hike finding before recent variation can reject a series."""
    if len(s.amounts) < PRICE_REGIME_OBSERVATIONS * 2:
        return None
    by_date = [a for _, a in sorted(zip(s.dates, s.amounts, strict=True))]
    split = len(by_date) - PRICE_REGIME_OBSERVATIONS
    old_window = by_date[split - PRICE_REGIME_OBSERVATIONS : split]
    new_window = by_date[split:]

    def stable(values: list[int]) -> bool:
        median = statistics.median(values)
        return median > 0 and max(values) - min(values) <= median * MAX_VARIATION

    if not stable(old_window) or not stable(new_window):
        return None
    old, new = statistics.median(old_window), statistics.median(new_window)
    if old <= 0 or new / old < HIKE_RATIO:
        return None
    previous, current = s.money(old), s.money(new)
    return _Draft(
        "hike",
        (key,),
        f"{_text(previous)} -> {_text(current)} per charge ({new / old:.1f}x)",
        s.money((new - old) * (30.44 / interval) * 12),
        amounts={"previous": previous, "current": current},
        facts={"ratio": round(new / old, 3), "interval_days": round(interval, 1)},
        series_monthly=s.money(new * (30.44 / interval)),
    )


def _has_future_projection(s: Series, today: date) -> bool:
    return any(p["posted_on"] > today.isoformat() for p in s.projected)


def _is_live(s: Series, today: date) -> bool:
    """A recurring series is live only when recent or projected forward."""
    if not _is_recurring(s):
        return False
    if _has_future_projection(s, today):
        return True
    return bool(
        s.last_charge and (today - s.last_charge).days <= s.interval_days * SILENT_INTERVALS
    )


def _publish(draft: _Draft, everything: Mapping[str, Series]) -> RecurringFinding:
    """Turn an internal draft into the result consumers see.

    This is where the series keys are dropped and replaced by safe references.
    """
    refs = [everything[key].ref for key in draft.keys]
    if draft.series_monthly is not None and refs:
        refs[0] = replace(refs[0], monthly=draft.series_monthly)
    return RecurringFinding(
        kind=draft.kind,
        series=tuple(refs),
        detail=draft.detail,
        annual_impact=draft.annual_impact,
        amounts=dict(draft.amounts),
        facts=dict(draft.facts),
    )


def analyse(rows: list[dict], today: date | None = None) -> list[RecurringFinding]:
    today = today or date.today()
    everything = _series(rows)
    live = {k: s for k, s in everything.items() if _is_live(s, today)}
    findings: list[_Draft] = []

    for key, s in sorted(everything.items()):
        interval = s.interval_days or 30
        silent = (today - s.last_charge).days if s.last_charge else 10**6

        # GHOST — projected but not actually charging.
        future = [p for p in s.projected if p["posted_on"] > today.isoformat()]
        if future and len(s.charges) >= MIN_GHOST_HISTORY and silent > interval * SILENT_INTERVALS:
            projected = Money(abs(money_from_row(future[0]).minor_units), s.currency)
            findings.append(
                _Draft(
                    "ghost",
                    (key,),
                    f"Simplifi projects {len(future)} future charges but nothing has "
                    f"cleared in {f'{silent} days' if s.last_charge else 'ever'} "
                    f"(last real charge: {s.last_charge or 'none'}). Forecast only "
                    f"— NOT money leaving your account.",
                    s.money(projected.minor_units * (365.25 / interval)),
                    amounts={"projected_charge": projected},
                    facts={
                        "projected_count": len(future),
                        "silent_days": None if s.last_charge is None else silent,
                        "last_charge": s.last_charge.isoformat() if s.last_charge else None,
                        "interval_days": round(interval, 1),
                    },
                )
            )
            continue

        if not _has_regular_cadence(s):
            continue

        # HIKE — detect a stable pre/post regime before recent variation can
        # reject the series because the price change itself is large.
        if hike := _hike(key, s, interval):
            findings.append(hike)

        if not _is_recurring(s):
            continue

        # LAPSED — was regular, stopped, nothing projected.
        if silent > interval * SILENT_INTERVALS and not future:
            findings.append(
                _Draft(
                    "lapsed",
                    (key,),
                    f"was every ~{interval:.0f}d, nothing since {s.last_charge} "
                    f"({silent} days). Confirm this was deliberate.",
                    s.money(-s.monthly_minor * 12),
                    amounts={"monthly": s.monthly},
                    facts={
                        "interval_days": round(interval, 1),
                        "silent_days": silent,
                        "last_charge": s.last_charge.isoformat() if s.last_charge else None,
                    },
                )
            )

    # ZOMBIE — a charge cleared after the newest projection ran out, i.e. the
    # series looked finished and then billed anyway.
    for key, s in live.items():
        if s.projected:
            newest_projection = max(p["posted_on"] for p in s.projected)
            after = [c for c in s.charges if c["posted_on"] > newest_projection]
            if after:
                findings.append(
                    _Draft(
                        "zombie",
                        (key,),
                        f"{len(after)} charge(s) cleared after the projected series "
                        f"ended {newest_projection} — billing outlived the schedule",
                        s.annual,
                        amounts={"monthly": s.monthly},
                        facts={
                            "charges_after_schedule": len(after),
                            "schedule_ended": newest_projection,
                        },
                    )
                )

    # TWIN — two live series sharing a name stem. Catches rebrands mid-flight
    # and per-person descriptors billed separately.
    # Match on a DISTINCTIVE shared token, not a prefix. Keying off the first
    # six characters grouped seven unrelated `direct_debit_*` series into one
    # "twin" finding — every bank-draft bill looks alike at the front.
    GENERIC = {
        "direct",
        "debit",
        "cash",
        "payment",
        "pay",
        "ach",
        "com",
        "online",
        "inc",
        "llc",
        "www",
        "subscription",
        "type",
    }
    by_token: dict[str, list[str]] = defaultdict(list)
    for key, series in live.items():
        for tok in series.merchant.split("_"):
            if len(tok) >= 5 and tok not in GENERIC and key not in by_token[tok]:
                by_token[tok].append(key)
    seen_pairs: set[tuple[str, ...]] = set()
    for tok, keys in by_token.items():
        if len(keys) < 2:
            continue
        sig = tuple(sorted(keys))
        if sig in seen_pairs:
            continue
        seen_pairs.add(sig)
        # Twins are per-account series, so two of them can be denominated
        # differently. Summing across currencies would produce a total in no
        # currency at all, so the shared one wins and the odd series out is
        # named as a fact rather than folded in silently.
        currency = live[sig[0]].currency
        shared_currency = [k for k in keys if live[k].currency == currency]
        total_minor = sum(live[k].monthly_minor for k in shared_currency)
        findings.append(
            _Draft(
                "twin",
                sig,
                f"{len(keys)} concurrent series both contain '{tok}' — one service "
                f"billed twice, or a rebrand still double-running",
                Money(round(total_minor * 12), currency),
                facts={
                    "shared_token": tok,
                    "series_count": len(keys),
                    "mixed_currency": len(shared_currency) != len(keys),
                },
            )
        )

    # SUCCESSOR — a series stopped and a same-priced one started within a month.
    # That is a rename, not a cancellation, and reporting it as `lapsed` would
    # be wrong twice over: the money did not stop, and the alert is noise.
    # SUCCESSOR — did something replace what stopped?
    #
    # THIS CHECK CANNOT BE MADE CONFIDENT ON AMOUNT AND TIMING ALONE, and three
    # rounds of tightening proved it. Each fix removed one coincidence and
    # surfaced the next:
    #
    #   hbomax -> aliexpress       (fixed: successor must start after)
    #   hbomax -> market_garden    (fixed: successor must be subscription-shaped)
    #   hbomax -> mta_nyct_paygo   (still there)
    #
    # In 1,876 rows something will always cost about $14 in the right week. So
    # the claim is split by the evidence available:
    #
    #   RENAMED  — names share a distinctive token. Confident.
    #              one provider descriptor -> another descriptor ('provider')
    #   LAPSED   — no name link. Reported as a stop, with the candidate named as
    #              a possibility, not an assertion.
    #
    # A rebrand with no shared token lands in the second bucket.
    # That is the honest outcome: a rebrand with no textual overlap is not
    # distinguishable from a cancellation without external knowledge, and
    # pretending otherwise would overstate the evidence.
    GENERIC_TOK = {
        "direct",
        "debit",
        "cash",
        "payment",
        "pay",
        "ach",
        "com",
        "online",
        "inc",
        "llc",
        "www",
        "subscription",
        "type",
        "sub",
    }
    candidates = {
        k: v for k, v in everything.items() if len(v.charges) >= 2 and v.variation <= MAX_VARIATION
    }

    for f in [f for f in findings if f.kind == "lapsed"]:
        old = everything[f.keys[0]]
        old_toks = {t for t in old.merchant.split("_") if len(t) >= 4 and t not in GENERIC_TOK}
        best = None
        for key, cand in candidates.items():
            if key == f.keys[0] or not cand.charges:
                continue
            started = min(cand.dates)
            gap = (started - old.last_charge).days if old.last_charge else 999
            if not (-7 <= gap <= 45):
                continue
            if not (
                old.monthly_minor > 0 and 0.8 <= cand.monthly_minor / old.monthly_minor <= 1.25
            ):
                continue
            shared = old_toks & {
                t for t in cand.merchant.split("_") if len(t) >= 4 and t not in GENERIC_TOK
            }
            if shared:
                findings.remove(f)
                findings.append(
                    _Draft(
                        "renamed",
                        (f.keys[0], key),
                        f"stopped {old.last_charge}; {cand.merchant} started {started} at "
                        f"{_text(cand.monthly)} (was {_text(old.monthly)}) "
                        f"and shares "
                        f"'{sorted(shared)[0]}'. Same service, new name — not a "
                        f"cancellation.",
                        # A rename moves money, it does not change how much: the
                        # successor bills what the predecessor billed, so there
                        # is no annual impact to report.
                        Money(0, old.currency),
                        amounts={"previous": old.monthly, "current": cand.monthly},
                        facts={
                            "shared_token": sorted(shared)[0],
                            "successor_started": started.isoformat(),
                            "confidence_basis": "shared_distinctive_token",
                        },
                    )
                )
                best = None
                break
            best = best or (key, started, cand)
        if best:
            key, started, cand = best
            f.detail += (
                f" Possibly replaced by {cand.merchant} (started {started}, "
                f"{_text(cand.monthly)}/mo) — similar price and timing "
                f"only, names unrelated, so treat as a guess."
            )
            # Recorded as a candidate, never as a second series on the finding:
            # the whole point of the `lapsed`/`renamed` split is that this one
            # is a guess, and putting the candidate in `series` would make it
            # look like established membership.
            f.facts["successor_candidate"] = cand.merchant
            f.facts["successor_started"] = started.isoformat()
            f.facts["successor_basis"] = "price_and_timing_only"

    order = {kind: index for index, kind in enumerate(FINDING_KINDS)}
    published = [_publish(draft, everything) for draft in findings]
    return sorted(
        published,
        key=lambda f: (order.get(f.kind, len(order)), -abs(f.annual_impact.minor_units)),
    )


def summary(rows: list[dict], today: date | None = None) -> str:
    today = today or date.today()
    live = {k: s for k, s in _series(rows).items() if _is_live(s, today)}
    # One total per currency. Minor units are only comparable within a
    # currency — ¥1,000 and $10.00 are both 1000 of them — so adding across
    # currencies produces a number that is correct in none of them. The first
    # version did exactly that and then labelled the result with whichever code
    # sorted first, while calling it the "majority" currency, which it also was
    # not. There is no exchange rate here and inventing one would be worse.
    totals: dict[str, float] = defaultdict(float)
    for series in live.values():
        totals[series.currency] += series.monthly_minor
    rendered = "; ".join(
        f"{_text(Money(round(minor), code))}/mo ({_text(Money(round(minor * 12), code))}/yr)"
        for code, minor in sorted(totals.items())
    )
    header = f"{len(live)} live subscriptions"
    if rendered:
        header += f", {rendered}"
    lines = [header, ""]
    for _key, s in sorted(live.items(), key=lambda kv: -kv[1].monthly_minor):
        lines.append(
            f"  {_text(s.monthly):>12}/mo  {s.merchant[:34]:34} "
            f"every ~{s.interval_days:.0f}d, last {s.last_charge}"
        )
    findings = analyse(rows, today)
    if findings:
        lines += ["", f"--- {len(findings)} thing(s) to look at ---"]
        for f in findings:
            lines.append(f"  [{f.kind:6}] {f.merchant[:40]}")
            lines.append(f"           {f.detail}")
    return "\n".join(lines)
