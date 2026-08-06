"""Read-only CLI for the reusable Simplifi transaction runtime."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from . import llm, prioritize, report, subscriptions
from .memory import MerchantMemory
from .secrets import SecretsError
from .semantics import is_settled
from .sources.csv_source import SchemaError, SimplifiCsvSource
from .store import Store


def _rows(db: Path) -> list[dict]:
    if not db.exists():
        raise ValueError(f"no database at {db}; run `ingest` first")
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM transaction_version WHERE is_current = 1 "
                "ORDER BY posted_on, transaction_id"
            )
        ]


def _latest_run(db: Path) -> tuple[int, str]:
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT id, source FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return (int(row[0]), str(row[1])) if row else (0, "unknown")


def _parse_today(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid date: {raw!r}; use YYYY-MM-DD") from exc


def _print_summary(records: list[dict], source: str) -> None:
    kinds = Counter(r["kind"] for r in records)
    rules = Counter(
        rule for r in records for rule in (r.get("norm_rules_applied") or "").split(",") if rule
    )
    print(f"INFO fetched {len(records)} rows from {source}")
    print(f"INFO accounting kinds: {dict(kinds.most_common())}")
    print(f"INFO excluded from statistics: {sum(r['poisons_statistics'] for r in records)}")
    print(f"INFO uncategorized: {sum(r['is_uncategorized'] for r in records)}")
    print(
        f"INFO foreign charges (issuer-converted): {sum(r['is_foreign_charge'] for r in records)}"
    )
    if rules:
        print(f"INFO normalization rules fired: {dict(rules.most_common())}")
    if source == "csv":
        print(
            "WARNING CSV has no settlement/projection state; recurring analysis is limited.",
            file=sys.stderr,
        )


def cmd_ingest(args: argparse.Namespace) -> int:
    if args.source == "csv":
        if not args.path:
            print("ERROR a CSV path is required with --source csv", file=sys.stderr)
            return 2
        path = Path(args.path)
        if not path.exists():
            print(f"ERROR no such file: {path}", file=sys.stderr)
            return 1
        try:
            records = SimplifiCsvSource(path).fetch()
        except (OSError, SchemaError, ValueError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 1
        detail = str(path)
    else:
        from .sources.api_source import ApiError, SimplifiApiSource, client_from_env_or_age

        try:
            client = client_from_env_or_age(verbose=args.verbose)
            records = SimplifiApiSource(
                client, date_on_after=args.since, modified_after=args.modified_after
            ).fetch()
        except (SecretsError, ApiError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 1
        detail = f"api since={args.since or 'all'} modified_after={args.modified_after or 'all'}"

    store = Store(Path(args.db))
    try:
        run_id = store.start_run(args.source, detail)
        outcomes = Counter(store.upsert_version(run_id, record) for record in records)
        store.record_accounts({r["account_name"] for r in records if r["account_name"]})
        store.finish_run(run_id, "success", len(records))
        store.commit()
    finally:
        store.close()

    print(f"INFO run {run_id}: versions {dict(outcomes)}")
    _print_summary(records, args.source)
    print(f"INFO wrote {args.db}")
    return 0


def _analysis_rows(args: argparse.Namespace) -> tuple[list[dict], int, str]:
    db = Path(args.db)
    rows = _rows(db)
    if not rows:
        raise ValueError(f"no current transactions in {db}; run `ingest` first")
    run_id, source = _latest_run(db)
    return rows, run_id, source


def _memory_proposals(rows: list[dict]) -> tuple[MerchantMemory, list[tuple[dict, object]]]:
    memory = MerchantMemory()
    memory.train(rows)
    pending = [
        (row, memory.propose(row))
        for row in rows
        if row["is_uncategorized"] and not row["poisons_statistics"]
        and is_settled(row)
    ]
    pending.sort(key=lambda pair: (pair[1] is None, -abs(pair[0]["amount_minor_units"])))
    return memory, pending


def cmd_analyze(args: argparse.Namespace) -> int:
    try:
        rows, run_id, source = _analysis_rows(args)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    today = _parse_today(args.today)
    memory, proposals = _memory_proposals(rows)
    prioritized = prioritize.analyse(rows, today)
    staleness = prioritize.activity_staleness(rows, today)
    findings = subscriptions.analyse(rows, today)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        report.render(
            run_id=run_id,
            source=source,
            rows=rows,
            prioritized=prioritized,
            staleness=staleness,
            proposals=proposals,
            memory_stats=memory.stats(),
            subscription_findings=findings,
        ),
        encoding="utf-8",
    )

    signals = Counter(signal.name for item in prioritized for signal in item.signals)
    resolved = sum(1 for _, proposal in proposals if proposal)
    print(f"INFO analysed {len(rows)} transactions")
    print(f"INFO flagged for review: {len(prioritized)}  signals: {dict(signals.most_common())}")
    print(
        f"INFO uncategorized: {len(proposals)}  memory resolved: {resolved}  "
        f"needs review/model: {len(proposals) - resolved}"
    )
    print(f"INFO recurring findings: {len(findings)}")
    print(f"INFO inactive accounts: {sum(s['status'] == 'stale' for s in staleness)}")
    if not any((row.get("txn_state") or "") for row in rows):
        print(
            "WARNING source has no settlement/projection state; recurring findings are limited.",
            file=sys.stderr,
        )
    print(f"INFO wrote {out}")
    return 0


def cmd_subs(args: argparse.Namespace) -> int:
    try:
        rows, _, _ = _analysis_rows(args)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    today = _parse_today(args.today)
    print(subscriptions.summary(rows, today))
    if not any((row.get("txn_state") or "") for row in rows):
        print(
            "\nWARNING source has no settlement/projection state; projected rows cannot be excluded.",
            file=sys.stderr,
        )
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    try:
        rows, _, _ = _analysis_rows(args)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    memory = MerchantMemory()
    memory.train(rows)
    residue = [
        row
        for row in rows
        if (
            row["is_uncategorized"]
            and not row["poisons_statistics"]
            and is_settled(row)
            and memory.propose(row) is None
        )
    ]
    if not residue:
        print("INFO nothing left for a model to propose")
        return 0

    accounts = {row["account_name"] for row in rows}
    taxonomy = sorted(
        {
            (row["category"] or "").strip()
            for row in rows
            if (row["category"] or "").strip() and not row["is_uncategorized"]
        }
        - accounts
    )
    examples = llm.build_examples(rows)
    backend_cls = llm.BACKENDS[args.model]
    backend = backend_cls()
    print(f"INFO {len(residue)} transactions need a proposal")
    print(f"INFO taxonomy: {len(taxonomy)} categories, {len(examples)} examples")
    try:
        proposals, usage, prompts = llm.classify(
            backend,
            residue,
            taxonomy,
            examples,
            chunk_size=args.chunk_size,
            dry_run=args.dry_run,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        out = Path(args.out).with_suffix(".prompt.txt")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n\n===== NEXT REQUEST =====\n\n".join(prompts), encoding="utf-8")
        print(f"INFO dry run: {len(prompts)} request(s); wrote {out}")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows_by_id = {row["transaction_id"]: row for row in residue}
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "transaction_id",
                "date",
                "payee",
                "amount",
                "proposed_category",
                "confidence",
                "rationale",
                "model",
                "decision",
            ]
        )
        for proposal in sorted(proposals, key=lambda item: -item.confidence):
            row = rows_by_id.get(proposal.transaction_id)
            if row is None:
                continue
            writer.writerow(
                [
                    proposal.transaction_id,
                    row["posted_on"],
                    row["payee_display"],
                    f"{row['amount_minor_units'] / 100:.2f}",
                    proposal.category or "",
                    f"{proposal.confidence:.2f}",
                    proposal.rationale,
                    proposal.model,
                    "",
                ]
            )
    resolved = sum(bool(proposal.category) for proposal in proposals)
    print(f"INFO model {backend.id}: {resolved}/{len(residue)} proposals with categories")
    print(f"INFO tokens in/out: {usage.input_tokens}/{usage.output_tokens}")
    print(f"INFO wrote {out}; proposals are not written back to Simplifi")
    return 0


def _api_client(args: argparse.Namespace):
    from .sources.api_source import ApiError, client_from_env_or_age

    try:
        return client_from_env_or_age(verbose=args.verbose)
    except (SecretsError, ApiError) as exc:
        raise ValueError(str(exc)) from exc


def cmd_probe(args: argparse.Namespace) -> int:
    from .sources.api_source import ApiError

    try:
        client = _api_client(args)
        profile = client.verify()
        accounts = client.accounts()
        logins = client.institution_logins()
    except (ValueError, ApiError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    print(f"INFO authenticated: profile keys {sorted(profile)[:8]}")
    print(
        f"INFO dataset {client.dataset_id[:8]}... · {len(accounts)} accounts · "
        f"{len(logins)} connections"
    )
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    from .sources.api_source import ApiError, schema_report

    try:
        print(schema_report(_api_client(args)))
    except (ValueError, ApiError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="load CSV or API transactions into SQLite")
    ingest.add_argument("path", nargs="?", help="CSV path when --source csv")
    ingest.add_argument("--source", choices=("csv", "api"), default="csv")
    ingest.add_argument("--since", help="API dateOnAfter value, YYYY-MM-DD")
    ingest.add_argument("--modified-after", help="API modifiedAfter cursor/value")
    ingest.add_argument("--db", default="simplifi.sqlite")
    ingest.add_argument("--verbose", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    analyze = sub.add_parser("analyze", help="build an HTML read-only review")
    analyze.add_argument("--db", default="simplifi.sqlite")
    analyze.add_argument("--out", default="report.html")
    analyze.add_argument("--today", help="analysis date, YYYY-MM-DD")
    analyze.set_defaults(func=cmd_analyze)

    classify = sub.add_parser("classify", help="propose categories for unresolved rows")
    classify.add_argument("--db", default="simplifi.sqlite")
    classify.add_argument("--out", default="proposals.csv")
    classify.add_argument("--model", choices=sorted(llm.BACKENDS), default="luna")
    classify.add_argument("--chunk-size", type=int, default=llm.CHUNK_SIZE)
    classify.add_argument(
        "--dry-run", action="store_true", help="write prompts without calling an API"
    )
    classify.set_defaults(func=cmd_classify)

    subs = sub.add_parser("subs", help="review recurring charges")
    subs.add_argument("--db", default="simplifi.sqlite")
    subs.add_argument("--today", help="analysis date, YYYY-MM-DD")
    subs.set_defaults(func=cmd_subs)

    probe = sub.add_parser("probe", help="verify API authentication and connection reads")
    probe.add_argument("--verbose", action="store_true")
    probe.set_defaults(func=cmd_probe)

    schema = sub.add_parser("schema", help="inspect API response shapes and pagination")
    schema.add_argument("--verbose", action="store_true")
    schema.set_defaults(func=cmd_schema)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
