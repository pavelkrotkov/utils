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
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise

from .semantics import is_projected, is_settled

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


@dataclass
class Series:
    merchant: str
    identity: str = ""
    charges: list[dict] = field(default_factory=list)
    projected: list[dict] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.merchant} [{self.identity}]" if self.identity else self.merchant

    @property
    def amounts(self) -> list[float]:
        return [abs(c["amount_minor_units"]) / 100 for c in self.charges]

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
    def monthly(self) -> float:
        """Normalised to a month, so annual plans compare with monthly ones."""
        interval = self.interval_days or 30
        return statistics.median(self.amounts) * (30.44 / interval)

    @property
    def recent_amounts(self) -> list[float]:
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


@dataclass
class Finding:
    kind: str
    merchant: str
    detail: str
    annual_impact: float = 0.0
    series_key: str | None = None


def _series(rows: list[dict]) -> dict[str, Series]:
    """Group into per-account merchant series.

    The account identity prevents two people or accounts billed by the same
    provider from being interleaved into one cadence. Rows without account
    metadata retain the historical merchant-only key for fixture and CSV
    compatibility.
    """
    out: dict[str, Series] = defaultdict(lambda: Series(""))
    for r in rows:
        if not is_settled(r) and not is_projected(r):
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
        identity = str(r.get("account_id") or r.get("account_name") or "").strip()
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
    if len(s.charges) < MIN_CHARGES:
        return False
    if not (MIN_DAYS <= s.interval_days <= MAX_DAYS):
        return False
    return s.variation <= MAX_VARIATION


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


def analyse(rows: list[dict], today: date | None = None) -> list[Finding]:
    today = today or date.today()
    everything = _series(rows)
    live = {k: s for k, s in everything.items() if _is_live(s, today)}
    findings: list[Finding] = []

    for key, s in sorted(everything.items()):
        interval = s.interval_days or 30
        silent = (today - s.last_charge).days if s.last_charge else 10**6

        # GHOST — projected but not actually charging.
        future = [p for p in s.projected if p["posted_on"] > today.isoformat()]
        if future and len(s.charges) >= MIN_GHOST_HISTORY and silent > interval * SILENT_INTERVALS:
            waste = abs(future[0]["amount_minor_units"]) / 100 * 12 if future else 0
            findings.append(
                Finding(
                    "ghost",
                    s.merchant,
                    f"Simplifi projects {len(future)} future charges but nothing has "
                    f"cleared in {f'{silent} days' if s.last_charge else 'ever'} "
                    f"(last real charge: {s.last_charge or 'none'}). Forecast only "
                    f"— NOT money leaving your account.",
                    waste,
                )
            )
            continue

        if not _is_recurring(s):
            continue

        # HIKE — compare the two most recent charges to the earlier median.
        if len(s.amounts) >= 4:
            by_date = [a for _, a in sorted(zip(s.dates, s.amounts, strict=True))]
            old, new = statistics.median(by_date[:-2]), statistics.median(by_date[-2:])
            if old > 0 and new / old >= HIKE_RATIO:
                findings.append(
                    Finding(
                        "hike",
                        s.merchant,
                        f"${old:,.2f} -> ${new:,.2f} per charge ({new / old:.1f}x)",
                        (new - old) * (30.44 / interval) * 12,
                    )
                )

        # LAPSED — was regular, stopped, nothing projected.
        if silent > interval * SILENT_INTERVALS and not future:
            findings.append(
                Finding(
                    "lapsed",
                    s.merchant,
                    f"was every ~{interval:.0f}d, nothing since {s.last_charge} "
                    f"({silent} days). Confirm this was deliberate.",
                    -s.monthly * 12,
                    key,
                )
            )

    # ZOMBIE — a charge cleared after the newest projection ran out, i.e. the
    # series looked finished and then billed anyway.
    for _key, s in live.items():
        if s.projected:
            newest_projection = max(p["posted_on"] for p in s.projected)
            after = [c for c in s.charges if c["posted_on"] > newest_projection]
            if after:
                findings.append(
                    Finding(
                        "zombie",
                        s.merchant,
                        f"{len(after)} charge(s) cleared after the projected series "
                        f"ended {newest_projection} — billing outlived the schedule",
                        s.monthly * 12,
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
            if len(tok) >= 5 and tok not in GENERIC:
                by_token[tok].append(key)
    seen_pairs: set[tuple[str, ...]] = set()
    for tok, keys in by_token.items():
        if len(keys) < 2:
            continue
        sig = tuple(sorted(keys))
        if sig in seen_pairs:
            continue
        seen_pairs.add(sig)
        total = sum(live[k].monthly for k in keys)
        labels = [live[k].label for k in keys]
        findings.append(
            Finding(
                "twin",
                " + ".join(labels),
                f"{len(keys)} concurrent series both contain '{tok}' — one service "
                f"billed twice, or a rebrand still double-running",
                total * 12,
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
        if f.series_key is None:
            continue
        old = everything[f.series_key]
        old_toks = {t for t in old.merchant.split("_") if len(t) >= 4 and t not in GENERIC_TOK}
        best = None
        for key, cand in candidates.items():
            if key == f.series_key or not cand.charges:
                continue
            started = min(cand.dates)
            gap = (started - old.last_charge).days if old.last_charge else 999
            if not (-7 <= gap <= 45):
                continue
            if not (old.monthly > 0 and 0.8 <= cand.monthly / old.monthly <= 1.25):
                continue
            shared = old_toks & {
                t for t in cand.merchant.split("_") if len(t) >= 4 and t not in GENERIC_TOK
            }
            if shared:
                findings.remove(f)
                findings.append(
                    Finding(
                        "renamed",
                        f"{old.merchant} -> {cand.merchant}",
                        f"stopped {old.last_charge}; {cand.merchant} started {started} at "
                        f"${cand.monthly:,.2f} (was ${old.monthly:,.2f}) and shares "
                        f"'{sorted(shared)[0]}'. Same service, new name — not a "
                        f"cancellation.",
                        0.0,
                    )
                )
                best = None
                break
            best = best or (key, started, cand)
        if best:
            key, started, cand = best
            f.detail += (
                f" Possibly replaced by {cand.merchant} (started {started}, "
                f"${cand.monthly:,.2f}/mo) — similar price and timing "
                f"only, names unrelated, so treat as a guess."
            )

    order = {"zombie": 0, "hike": 1, "twin": 2, "renamed": 3, "ghost": 4, "lapsed": 5}
    return sorted(findings, key=lambda f: (order.get(f.kind, 9), -abs(f.annual_impact)))


def summary(rows: list[dict], today: date | None = None) -> str:
    today = today or date.today()
    live = {k: s for k, s in _series(rows).items() if _is_live(s, today)}
    lines = [
        f"{len(live)} live subscriptions, "
        f"${sum(s.monthly for s in live.values()):,.2f}/mo "
        f"(${sum(s.monthly for s in live.values()) * 12:,.2f}/yr)",
        "",
    ]
    for _key, s in sorted(live.items(), key=lambda kv: -kv[1].monthly):
        lines.append(
            f"  ${s.monthly:>8,.2f}/mo  {s.merchant[:34]:34} "
            f"every ~{s.interval_days:.0f}d, last {s.last_charge}"
        )
    findings = analyse(rows, today)
    if findings:
        lines += ["", f"--- {len(findings)} thing(s) to look at ---"]
        for f in findings:
            lines.append(f"  [{f.kind:6}] {f.merchant[:40]}")
            lines.append(f"           {f.detail}")
    return "\n".join(lines)
