"""Self-contained HTML report with no CDN or JavaScript dependencies."""

from __future__ import annotations

import html
from datetime import date

from .memory import Proposal
from .semantics import is_statistics_quarantined

CSS = """
:root { --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#e4e4e7; --accent:#4f39d9;
        --warn:#b45309; --bad:#b91c1c; --ok:#15803d; --chip:#f4f4f5; }
* { box-sizing:border-box; }
body { font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       color:var(--fg); background:var(--bg); margin:0; padding:32px; max-width:1100px; }
h1 { font-size:26px; margin:0 0 4px; }
h2 { font-size:19px; margin:36px 0 10px; padding-bottom:6px; border-bottom:2px solid var(--line); }
.sub { color:var(--muted); margin-bottom:24px; }
table { border-collapse:collapse; width:100%; font-size:14px; }
th { text-align:left; font-weight:600; color:var(--muted); border-bottom:1px solid var(--line);
     padding:8px 10px; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
td { padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:hover td { background:#fafafa; }
.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.chip { display:inline-block; background:var(--chip); border-radius:4px; padding:1px 7px;
        font-size:12px; margin:1px 3px 1px 0; white-space:nowrap; }
.chip.bad { background:#fee2e2; color:var(--bad); }
.chip.warn { background:#fef3c7; color:var(--warn); }
.chip.ok { background:#dcfce7; color:var(--ok); }
.ev { color:var(--muted); font-size:12px; }
.cards { display:flex; gap:12px; flex-wrap:wrap; margin:16px 0 8px; }
.card { border:1px solid var(--line); border-radius:8px; padding:12px 16px; min-width:150px; }
.card .k { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
.card .v { font-size:22px; font-weight:600; margin-top:2px; }
code { background:var(--chip); padding:2px 6px; border-radius:4px; font-size:13px; }
details { margin-top:8px; }
summary { cursor:pointer; color:var(--accent); font-size:14px; }
.empty { color:var(--muted); font-style:italic; padding:12px 0; }
"""


def _e(v) -> str:
    return html.escape(str(v))


def _money(minor: int) -> str:
    return f"{minor / 100:,.2f}"


def _evidence(ev: dict) -> str:
    return " ".join(f'<span class="ev">{_e(k)}={_e(v)}</span>' for k, v in ev.items())


def _provenance(row: dict) -> str:
    return "<br>".join(
        _e(f"{label}={row.get(key, 'unknown')}")
        for key, label in (
            ("transaction_id", "transaction_id"),
            ("id", "version_id"),
            ("run_id", "run_id"),
            ("source_hash", "source_hash"),
            ("algorithm_version", "algorithm_version"),
            ("ruleset_version", "ruleset_version"),
        )
    )


def render(
    *,
    run_id: int,
    source: str,
    analysis_date: date | str | None = None,
    rows: list[dict],
    prioritized: list,
    staleness: list[dict],
    proposals: list[tuple[dict, Proposal | None]],
    memory_stats: dict,
    subscription_findings: list | None = None,
    limitations: list[str] | None = None,
) -> str:
    total = len(rows)
    uncat = sum(1 for r in rows if r["is_uncategorized"])
    excluded = sum(1 for r in rows if is_statistics_quarantined(r))
    stale = [s for s in staleness if s["status"] == "stale"]

    p: list[str] = []
    p.append(f"<!doctype html><meta charset=utf-8><title>Transaction review — run {run_id}</title>")
    p.append(f"<style>{CSS}</style>")
    p.append(
        f"<h1>Transaction review</h1><div class=sub>run {run_id} · source {_e(source)} · "
        f"generated {date.today().isoformat()} · analysis through "
        f"{_e(analysis_date or date.today().isoformat())} · "
        "row-level provenance shown below · "
        "no inference used</div>"
    )
    for limitation in limitations or []:
        p.append(f"<div class='chip warn'>LIMITATION: {_e(limitation)}</div>")

    # 1. Run health
    p.append("<h2>Run health</h2><div class=cards>")
    for k, v in (
        ("Transactions", f"{total:,}"),
        ("Needs a category", str(uncat)),
        ("Excluded from stats", str(excluded)),
        ("Flagged for review", str(len(prioritized))),
        ("Inactive accounts", str(len(stale))),
    ):
        p.append(f"<div class=card><div class=k>{_e(k)}</div><div class=v>{_e(v)}</div></div>")
    p.append("</div>")

    p.append(
        "<table><tr><th>Account</th><th>Last transaction</th>"
        "<th class=num>Days</th><th>Status</th></tr>"
    )
    for s in staleness:
        cls = "bad" if s["days_stale"] > 60 else "warn" if s["status"] == "stale" else "ok"
        p.append(
            f"<tr><td>{_e(s['account'])}</td><td>{_e(s['last_transaction'])}</td>"
            f"<td class=num>{s['days_stale']}</td>"
            f"<td><span class='chip {cls}'>{_e(s['status'])}</span></td></tr>"
        )
    p.append("</table>")

    # 2. Needs attention
    p.append("<h2>Needs attention</h2>")
    if not prioritized:
        p.append("<div class=empty>Nothing flagged.</div>")
    else:
        p.append(
            "<table><tr><th>Date</th><th>Payee</th><th>Account</th>"
            "<th class=num>Amount</th><th>Signals</th><th class=num>Score</th>"
            "<th>Provenance</th></tr>"
        )
        for item in prioritized:
            r = item.row
            chips = "".join(
                f'<span class="chip {"bad" if s.score >= 3 else "warn" if s.score >= 1 else ""}">'
                f"{_e(s.name)}</span>"
                for s in item.signals
            )
            ev = "<br>".join(_evidence(s.evidence) for s in item.signals)
            p.append(
                f"<tr><td>{_e(r['posted_on'])}</td><td>{_e(r['payee_display'])}</td>"
                f"<td>{_e(r['account_name'])}</td>"
                f"<td class=num>{_money(r['amount_minor_units'])}</td>"
                f"<td>{chips}<br>{ev}</td>"
                f"<td class=num>{item.total_score}</td><td class=ev>{_provenance(r)}</td></tr>"
            )
        p.append("</table>")

    # 3. Subscription review
    findings = list(subscription_findings or [])
    p.append("<h2>Recurring-charge review</h2>")
    p.append(
        "<div class=sub>Recurring findings use cleared activity where the source exposes "
        "settlement state; projected rows are not treated as charges.</div>"
    )
    if not findings:
        p.append("<div class=empty>No recurring-charge findings.</div>")
    else:
        p.append(
            "<table><tr><th>Finding</th><th>Merchant</th>"
            "<th>Detail</th><th class=num>Annual impact</th></tr>"
        )
        for finding in findings:
            p.append(
                f"<tr><td><span class='chip warn'>{_e(finding.kind)}</span></td>"
                f"<td>{_e(finding.merchant)}</td><td>{_e(finding.detail)}</td>"
                f"<td class=num>{_money(round(finding.annual_impact * 100))}</td></tr>"
            )
        p.append("</table>")

    # 4. Needs a category
    p.append("<h2>Needs a category</h2>")
    if not proposals:
        p.append("<div class=empty>Nothing uncategorized, or no confident proposals.</div>")
    else:
        p.append(
            "<table><tr><th>Date</th><th>Payee</th><th class=num>Amount</th>"
            "<th>Proposed</th><th>Basis</th><th>Provenance</th></tr>"
        )
        for r, prop in proposals:
            basis = (
                _evidence(prop.evidence)
                if prop
                else '<span class="ev">no history — needs a model or manual review</span>'
            )
            cat = _e(prop.category) if prop else "—"
            p.append(
                f"<tr><td>{_e(r['posted_on'])}</td><td>{_e(r['payee_display'])}</td>"
                f"<td class=num>{_money(r['amount_minor_units'])}</td>"
                f"<td>{cat}</td><td>{basis}</td><td class=ev>{_provenance(r)}</td></tr>"
            )
        p.append("</table>")

    p.append(f"<h2>Memory</h2><div class=sub>{_e(memory_stats)}</div>")
    return "\n".join(p)
