# Return-stacked ETFs vs. managed-futures funds: what they are, how they’re taxed, and how to compare them

## 1) What “return stacking” ETFs are
**Return-stacked (“portable alpha”-style) ETFs** aim to deliver roughly **$1 of equity exposure + $1 of managed-futures exposure per $1 invested** (i.e., **~200% gross notional**). They typically hold **cash/T-bills as collateral** and use **derivatives (futures/swaps)** to overlay exposures, so the same capital supports multiple sleeves.

**Key idea:** you keep “full equity” exposure while adding a diversifier (managed futures) without selling down equities.

## 2) Why investors consider them (advantages)
- **Capital-efficient diversification:** keep ~100% equity exposure and add managed futures “on top.”
- **Potential regime diversification:** managed futures (trend/carry variants) can help in some non-equity-friendly regimes (not guaranteed).
- **Operational simplicity:** one ticker manages collateral, margin, roll, rebalancing.

## 3) Main risks / disadvantages
- **It’s leverage in gross exposure:** returns can amplify in both directions; correlations can spike.
- **Fees + implementation frictions:** managed futures is trading-heavy; stacking adds complexity and cost vs plain beta.
- **Model risk:** “managed futures” varies a lot by manager (signals, speed, universe, risk targeting).
- **Behavior risk:** long stretches of underperformance vs equities can be hard to hold.

## 4) Managed futures landscape (and who competes with whom)
There are two relevant product “families”:
1) **Stacked / multi-sleeve products**: combine equity (or bonds) + managed futures in one wrapper.
2) **Managed-futures-only ETFs / mutual funds**: you pair them yourself with equities.

The original post you referenced mentioned big systematic managers like **Aspect** and **AQR**—the point being: managed futures is a mature institutional strategy; packaging and tax implementation are becoming key differentiators.

## 5) What asset classes “managed futures” typically spans
Most managed-futures implementations trade futures across:
- **Equity index futures** (e.g., S&P 500, global indices)
- **Rates / fixed income futures** (Treasuries, global bonds)
- **FX** (major currencies)
- **Commodities** (energy, metals, ags)

The exact contract set differs materially by product.

## 6) Tax forms: 1099 vs K-1 (and what “ETF-like tax treatment” means)
- **Normal index ETFs (e.g., VOO)** generally report on **Form 1099** (1099-DIV; and your broker reports sales on 1099-B). They typically **do not issue K-1s**.
- **K-1s** usually show up when the vehicle is structured as a **partnership/commodity pool** (common in some commodity products), not plain equity index ETFs.

### Futures tax mechanics (the “moving part” people miss)
Many regulated futures are **Section 1256 contracts**:
- **Marked-to-market** annually (taxable results recognized each year)
- Typically **60% long-term / 40% short-term** capital gain/loss treatment
- **Wash-sale rules generally don’t apply** to 1256 contracts

This can create taxable distributions even without selling shares. Wrapper choice (ETF vs mutual fund) doesn’t remove 1256 economics; it mainly affects whether the vehicle uses in-kind mechanisms (ETF advantage) and how well it can **harvest/offset** taxable results.

### AQR “ETF-like tax treatment” claim in the thread
The idea: AQR’s tax-aware management for certain long/short / managed-futures mutual funds (example discussed: **QMHIX**) reportedly reduced distributions dramatically. This is “ETF-like” in the sense of **low annual taxable distributions**, **not** because the mutual fund suddenly became an ETF with in-kind redemptions.

## 7) How to think about risk: is “beta > 1”?
Return stacking is better modeled as:
- **~1.0× equity exposure + 1.0× managed-futures exposure + collateral yield**
Not “equity beta = 2.” Total portfolio volatility can be higher or lower than equities depending on correlation and the managed-futures sleeve behavior. In equity selloffs, managed futures may help or may not—depends on the strategy and the market regime.

## 8) Practical placement in an individual portfolio
- If you use stacked ETFs in taxable, expect **more tax activity** than a plain equity index ETF (due to the futures sleeve), though many products aim for **1099** reporting.
- Many investors prefer placing managed-futures-like exposures in **tax-advantaged accounts** if available; in taxable, scrutinize historical distributions and the fund’s tax design.
- Consider **risk-budgeting**: often it’s sensible to **replace** some equity with a stacked product rather than layering it on top of a full equity allocation.

---

## Comparison table: returns, fees, and tax form (best-effort with available history)
> Source: FT Markets via `factor_fund_performance.py`. N/A indicates insufficient history.

| Fund (Ticker) | Type | 1M | 3M | 6M | 1Y | 3Y | 5Y | Fee (expense ratio) | 1099 or K-1? |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| Vanguard S&P 500 ETF (**VOO**) | US large-cap equity index | +0.06% | +2.65% | +10.98% | +17.84% | +22.97% | +14.38% | 0.03% | 1099 |
| WisdomTree U.S. Efficient Core Fund (**NTSX**) | Efficient core: ~90% US stocks + ~60% Treasuries (futures overlay) | -0.38% | +2.31% | +10.47% | +18.81% | +20.63% | +9.76% | 0.20% | 1099 |
| WisdomTree International Efficient Core Fund (**NTSI**) | Efficient core: ~90% international stocks + ~60% Treasuries (futures overlay) | +2.26% | +4.75% | +10.28% | +30.48% | +15.24% | N/A | 0.26% | 1099 |
| WisdomTree Emerging Markets Efficient Core Fund (**NTSE**) | Efficient core: ~90% emerging markets stocks + ~60% Treasuries (futures overlay) | +3.11% | +6.07% | +18.09% | +37.75% | +16.40% | N/A | 0.32% | 1099 |
| AQR Managed Futures Strategy HV Fund Class I (**QMHIX**) | Managed futures / trend (mutual fund) | +3.76% | +4.08% | +14.64% | +19.98% | +9.88% | +14.27% | 3.87% | 1099 |
| AQR Trend Total Return Fund Class N (**QNZNX**) | “Trend / multi-asset leveraged” (mutual fund) | +2.22% | +4.67% | +15.67% | +22.88% | +26.77% | N/A | 3.07% | 1099 |
| Return Stacked US Stocks & Managed Futures ETF (**RSST**) | **Stacked**: ~100% stocks + ~100% managed futures | +2.72% | +8.91% | +22.96% | +20.95% | N/A | N/A | 0.99% | (ETF; typically 1099) |
| Return Stacked Bonds & Managed Futures ETF (**RSBT**) | **Stacked**: ~100% bonds + ~100% managed futures | +1.58% | +6.51% | +13.42% | +10.43% | N/A | N/A | 1.02% | (ETF; typically 1099) |
| Return Stacked Global Stocks & Bonds ETF (**RSSB**) | **Stacked**: ~100% global stocks + ~100% Treasuries | +1.07% | +3.88% | +12.22% | +26.21% | N/A | N/A | 0.40% | (ETF; typically 1099) |
| Return Stacked U.S. Stocks & Futures Yield ETF (**RSSY**) | **Stacked**: ~100% stocks + ~100% futures carry | -3.51% | -1.98% | +6.62% | -2.97% | N/A | N/A | 0.98% | (ETF; typically 1099) |
| Simplify Managed Futures Strategy ETF (**CTA**) | Managed futures ETF | +0.11% | -1.10% | +4.11% | +1.31% | +6.67% | N/A | 0.75% | **1099 (no K-1)** |
| KraneShares Mount Lucas Managed Futures ETF (**KMLM**) | Managed futures ETF | +2.46% | +1.20% | +3.35% | -3.21% | -3.44% | +4.80% | 0.90% | **1099 (no K-1)** |

---

### Key takeaways
- **Return-stacked ETFs** add a diversifier without selling equities, but increase complexity and can raise total portfolio risk/volatility.
- **Tax:** plain index ETFs are typically **1099**; futures-driven strategies can create annual taxable effects, but some managers are improving tax outcomes substantially.
- **Decision lens:** choose between (a) **one-ticker stacked** convenience vs (b) **separate sleeves** (equity ETF + managed futures fund/ETF) for better tax-location and sizing control.

---

## Return Stacked ETF notes (RSST / RSBT / RSSB)
**Strategy + composition**
- **RSST:** ~100% U.S. equities + ~100% managed-futures trend sleeve.
- **RSBT:** ~100% U.S. bonds + ~100% managed-futures trend sleeve.
- **RSSB:** ~100% global equities + ~100% U.S. Treasuries (no managed-futures sleeve).
- **RSSY:** ~100% U.S. equities + ~100% futures yield/carry sleeve (not trend).

**Provider snapshot (Return Stacked ETFs site, NAV performance as of 12/31/2025)**
- **RSST NAV:** 1M 1.89%, 3M 8.03%, 6M 21.96%, 1Y 19.97%, since inception 44.44%.
- **RSBT NAV:** 1M 1.58%, 3M 6.51%, 6M 13.42%, 1Y 10.43%, since inception -5.14%.
- **RSSB NAV:** 1M 0.17%, 3M 2.97%, 6M 11.23%, 1Y 25.10%, since inception 47.76%.
- **RSSY NAV:** 1M -3.51%, 3M -1.98%, 6M 6.62%, 1Y -2.97%, since inception -1.41%.

**Why performance differed (mechanics, not promises)**
- **Equity vs bond beta:** RSST and RSSB are equity-heavy; RSBT is bond-heavy, so equity-led periods tend to favor RSST/RSSB.
- **Trend vs carry sleeve impact:** RSST/RSBT use trend; RSSY uses futures yield/carry. These sleeves can behave differently across regimes, and RSSB has no managed-futures sleeve at all.
- **Efficient Core contrast:** NTSX/NTSI/NTSE use equity + Treasury futures overlays (capital-efficient beta), not managed futures, so their behavior is closer to equity+duration than trend/carry.
- **Rates regime exposure:** RSBT and RSSB carry large rate sensitivity (bonds vs Treasuries), which can dominate outcomes when rates move.

**Provider / roles**
- **Platform:** Return Stacked ETFs.
- **Investment adviser:** Tidal Investments, LLC.
- **Sub-adviser:** Newfound Research LLC.
- **Futures trading advisor:** ReSolve Asset Management SEZC (Cayman).
- **Distributor:** Foreside Fund Services, LLC.

**Data note**
- The provider site publishes month-end summary tables (1M/3M/6M/YTD/1Y) but not a full monthly time series. For deeper attribution, a separate NAV history source is needed.
