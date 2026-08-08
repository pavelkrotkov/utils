"""Read-only CLI for the reusable Simplifi transaction runtime."""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sqlite3
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import (
    artifacts,
    decisions,
    judgment_examples,
    llm,
    prioritize,
    report,
    review_packet,
    subscriptions,
)
from .memory import MerchantMemory, Proposal
from .secrets import SecretsError
from .semantics import (
    SOURCE_CAPABILITIES,
    assess_eligibility,
    is_projected,
    is_statistics_eligible,
    is_statistics_quarantined,
)
from .sources.csv_source import SchemaError, SimplifiCsvSource
from .store import RUN_ABORTED, RUN_FAILED, RUN_STARTED, RUN_SUCCEEDED, Store
from .sync_scope import SyncScope, api_scope, scope_from_profile

#: Arguments that name an artifact this runtime owns, and therefore places.
#: The ingest CSV is deliberately absent: it is the user's export, read where
#: they put it, and relocating their input would be a surprise rather than a
#: protection.
ARTIFACT_ARGS = ("db", "out", "packet_out", "packet", "proposals")


def _allow_unsafe(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "allow_unsafe_paths", False)) or artifacts.allow_unsafe_from_env()


def _prepare_paths(args: argparse.Namespace) -> Path:
    """Resolve every artifact argument against the data directory, in place.

    Done once at the top of each command rather than at each write, so a run
    that is going to refuse a location refuses it before opening a database,
    contacting an API, or appending anything immutable. Rewriting the values on
    `args` keeps the rest of each command reading `args.db` as before; the
    difference is that by then it is an absolute, vetted path.
    """
    allow_unsafe = _allow_unsafe(args)
    data_dir = artifacts.prepare_data_dir(
        getattr(args, "data_dir", None), allow_unsafe=allow_unsafe
    )
    for attr in ARTIFACT_ARGS:
        value = getattr(args, attr, None)
        if value is None:
            continue
        resolved = artifacts.resolve_artifact(
            value,
            data_dir,
            allow_unsafe=allow_unsafe,
            label=f"--{attr.replace('_', '-')}",
        )
        setattr(args, attr, str(resolved))
    return data_dir


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


MAX_CURSOR_FUTURE_SKEW = timedelta(minutes=5)


def _parse_cursor_timestamp(raw: str) -> datetime:
    value = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid cursor timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed > datetime.now(timezone.utc) + MAX_CURSOR_FUTURE_SKEW:
        raise ValueError(f"cursor timestamp is too far in the future: {value!r}")
    return parsed


def _next_cursor(as_of: str | None, floor: str | None = None) -> tuple[str | None, str | None]:
    """Turn the response's `metaData.asOf` into the next cursor, or refuse to.

    Returns ``(cursor, warning)``. A None cursor leaves ``cursor_after`` unset
    on the run, so :meth:`Store.latest_cursor` keeps returning the last value
    that was actually trustworthy and the following run re-requests the window.

    This replaces advancing by ``max(modifiedAt)`` over the returned rows. That
    was wrong in a way that loses data rather than merely being imprecise: the
    rows are what the server chose to return, so the maximum over them can sit
    *ahead* of records the server has not yet made visible, and the next
    request skips straight past them. ``asOf`` is the server's own statement of
    what the payload covers, which is the only claim we are entitled to make.

    ``floor`` is the cursor this run already stood on. The cursor is a
    watermark and only ever moves forward: a stale read replica or a clock
    rollback can hand back an older but perfectly well-formed ``asOf``, and
    recording it would drag the watermark backwards. That is not merely a
    wasted refetch — if the replica stays behind, every run rewinds again and
    the incremental sync never converges. Keeping the floor is also safe:
    the request was made with ``modifiedAfter=floor``, so the server returned
    everything it had past that point regardless of what its marker claims.
    """
    if not as_of:
        return None, (
            "API response did not supply a usable metaData.asOf; the synchronization "
            "cursor was left unchanged and the next run will re-request this window"
        )
    try:
        parsed = _parse_cursor_timestamp(as_of)
    except ValueError as exc:
        return None, (
            f"{exc} in response metaData.asOf; the synchronization cursor was left "
            "unchanged and the next run will re-request this window"
        )
    if floor:
        try:
            parsed_floor = _parse_cursor_timestamp(floor)
        except ValueError:
            # An unusable floor cannot order anything, so it cannot veto. The
            # marker we did parse is still the best claim available.
            return as_of, None
        if parsed < parsed_floor:
            return None, (
                f"API response metaData.asOf {as_of!r} predates the cursor this run "
                f"already held ({floor!r}), which suggests a stale replica or a clock "
                "rollback; the synchronization cursor was left unchanged rather than "
                "moved backwards"
            )
    return as_of, None


def _aggregator_health(
    login: dict,
    now: datetime | None = None,
    expected_refresh_days: float | None = None,
) -> list[dict]:
    """Return provider-health observations for one institution login."""
    now = now or datetime.now(timezone.utc)
    name = str(login.get("name") or login.get("id") or "unknown")
    aggregators = login.get("aggregators") or []
    if not aggregators:
        return [{"name": name, "status": "unknown", "issues": ["no aggregator data"]}]

    out = []
    for aggregator in aggregators:
        status = str(aggregator.get("aggStatus") or "unknown")
        code = str(aggregator.get("aggStatusCode") or "")
        detail = str(aggregator.get("aggStatusDetail") or "")
        last_success = aggregator.get("lastRefreshSuccessfulAt")
        issues = []
        if status.upper() != "OK":
            issues.append("status is not OK")
        if code:
            issues.append("care code present")
        age_days = None
        if last_success:
            try:
                refreshed = datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
                if refreshed.tzinfo is None:
                    refreshed = refreshed.replace(tzinfo=timezone.utc)
                age_days = (now - refreshed).total_seconds() / 86400
                if expected_refresh_days is not None and age_days > expected_refresh_days:
                    issues.append("last successful refresh is stale")
            except ValueError:
                issues.append("last successful refresh has an invalid timestamp")
        else:
            issues.append("no successful refresh recorded")
        out.append(
            {
                "name": name,
                "status": status,
                "code": code,
                "detail": detail,
                "last_success": last_success or "never",
                "age_days": age_days,
                "next_manual": aggregator.get("nextManualRefreshEligibleAt") or "unknown",
                "issues": issues,
            }
        )
    return out


def _rows(db: Path, source: str) -> list[dict]:
    if not db.exists():
        raise ValueError(f"no database at {db}; run `ingest` first")
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM transaction_version WHERE is_current = 1 "
                "AND source = ? ORDER BY posted_on, transaction_id",
                (source,),
            )
        ]


def _latest_run(db: Path) -> tuple[int, str]:
    """The newest succeeded run, opening through `Store` so migrations run.

    Deliberately not a raw `sqlite3.connect`. A read-only command can be the
    first thing invoked after an upgrade, and querying a column a pending
    migration has not added yet fails with `no such column` — an error about
    our own schema, reported to someone who did nothing wrong. Going through
    `Store` means any command can upgrade a database, not just `ingest`.
    """
    store = Store(db)
    try:
        return store.latest_successful_run()
    finally:
        store.close()


def _parse_today(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid date: {raw!r}; use YYYY-MM-DD") from exc


def _print_summary(records: list[dict], source: str) -> None:
    active_records = [r for r in records if not r.get("is_deleted")]
    kinds = Counter(r["kind"] for r in active_records)
    rules = Counter(
        rule
        for r in active_records
        for rule in (r.get("norm_rules_applied") or "").split(",")
        if rule
    )
    print(f"INFO fetched {len(records)} rows from {source} ({len(active_records)} active)")
    print(f"INFO accounting kinds: {dict(kinds.most_common())}")
    print(f"INFO deleted tombstones: {sum(bool(r.get('is_deleted')) for r in records)}")
    print(
        "INFO excluded from statistics: "
        f"{sum(is_statistics_quarantined(r) for r in active_records)}"
    )
    print(f"INFO uncategorized: {sum(r['is_uncategorized'] for r in active_records)}")
    print(
        "INFO review eligible: "
        f"{sum(r.get('review_eligible', assess_eligibility(r).eligible) for r in active_records)}"
        f"/{len(active_records)}"
    )
    print(
        "INFO foreign charges (issuer-converted): "
        f"{sum(r['is_foreign_charge'] for r in active_records)}"
    )
    if rules:
        print(f"INFO normalization rules fired: {dict(rules.most_common())}")
    if source == "csv":
        print(
            "WARNING CSV has no settlement/projection state; recurring analysis is limited.",
            file=sys.stderr,
        )


#: Failures the runtime raises deliberately, whose messages are already written
#: to tell an operator what to do. Anything outside this set is a surprise, and
#: its message gets the context a bare `KeyError: 'amount'` does not carry.
def _expected_failures() -> tuple[type[BaseException], ...]:
    from .sources.api_source import ApiError

    return (ApiError, SecretsError, SchemaError, OSError, sqlite3.Error, ValueError)


def _failure_message(exc: BaseException) -> str:
    """An actionable description of why a run stopped.

    Stored alongside the class rather than instead of it: the class aggregates
    ("how often is this an AuthError?"), the message tells one person what to
    do next. A record with only `KeyError` in it answers neither question.
    """
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return (
            "the run was interrupted before it finished; no partial data was kept, "
            "and the synchronization cursor was not advanced. Re-run to continue."
        )
    detail = str(exc).strip() or repr(exc)
    if isinstance(exc, _expected_failures()):
        return detail
    return (
        f"{detail} — an unexpected {type(exc).__name__} escaped the ingest path, so the "
        "run recorded no data and the cursor was not advanced. This usually means a "
        "provider response no longer matches the shape the adapter expects; re-run with "
        "--verbose, and compare against `schema` output if it repeats."
    )


def _finalize_failed_run(store: Store, run_id: int, state: str, exc: BaseException | None) -> None:
    """Record why a run stopped, without letting the bookkeeping mask it.

    This runs while something has already gone wrong, and the database is one
    of the things that may be wrong. If recording the failure itself fails, say
    so and let the original exception continue — replacing a clear provider
    error with a confusing SQLite one, three frames from its cause, is exactly
    how a five-minute diagnosis becomes an afternoon.
    """
    try:
        store.rollback()
        transitioned = store.finish_run(
            run_id,
            state,
            0,
            error_class=type(exc).__name__ if exc is not None else None,
            error_message=_failure_message(exc) if exc is not None else None,
        )
        store.commit()
        if not transitioned:
            # The run had already finished, so this error struck after its data
            # was committed. Saying so is the useful diagnostic; rewriting the
            # run as failed would contradict rows that are legitimately current.
            print(
                f"WARNING run {run_id} had already completed; the error above occurred "
                "afterwards and did not affect the data it committed.",
                file=sys.stderr,
            )
    except sqlite3.Error as bookkeeping:
        print(
            f"WARNING could not record run {run_id} as {state}: {bookkeeping}. "
            "The run remains 'started' and will not be used for analysis.",
            file=sys.stderr,
        )


@contextmanager
def _run_finalized(store: Store, run_id: int):
    """Guarantee every exit path leaves the run in a terminal state.

    Without this an unexpected exception left the run at NULL outcome forever:
    not running, not failed, not anything a query could act on. The catch is
    deliberately `BaseException` — the surprises worth protecting against are
    precisely the ones no `except Exception` was written for.

    Interruption is separated from failure because they call for different
    responses. `aborted` means someone stopped it and can start it again;
    `failed` means something is wrong and re-running alone may not help.
    """
    try:
        yield
    except (KeyboardInterrupt, SystemExit) as exc:
        _finalize_failed_run(store, run_id, RUN_ABORTED, exc)
        raise
    except BaseException as exc:
        _finalize_failed_run(store, run_id, RUN_FAILED, exc)
        raise


def _is_complete_snapshot(args: argparse.Namespace) -> bool:
    return args.source == "csv" or (args.source == "api" and args.full_rescan and not args.since)


def _ensure_model_key(model: str, verbose: bool) -> None:
    key = llm.REQUIRED_API_KEYS[model]
    if os.environ.get(key):
        return
    from .secrets import load_into_env

    load_into_env(required=[key], verbose=verbose)


def cmd_ingest(args: argparse.Namespace) -> int:
    try:
        data_dir = _prepare_paths(args)
    except artifacts.ArtifactError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    if args.verbose:
        print(f"INFO {artifacts.describe_policy(data_dir)}")
    store = Store(Path(args.db))
    mode = "full-rescan" if args.full_rescan else "incremental"
    if args.source == "csv":
        detail = f"csv path={args.path or 'missing'}"
    else:
        # The scope needs a live client, which does not exist yet — the run row
        # is opened first so an auth failure is still recorded as a failed run.
        # Cursor selection therefore happens after the client is up, and the
        # resolved scope is written back onto the run.
        detail = f"api since={args.since or 'all'} mode={mode} scope=unresolved"
    run_id = store.start_run(args.source, detail)
    store.commit()
    try:
        with _run_finalized(store, run_id):
            return _ingest_within_run(args, store, run_id, mode)
    finally:
        store.close()


def _ingest_within_run(args: argparse.Namespace, store: Store, run_id: int, mode: str) -> int:
    """The body of an ingest, with the run guaranteed to reach a terminal state.

    Split out so every path — including one that raises something nobody
    anticipated — passes back through the caller's guard. Inlining this again
    would restore the original bug the first time a new `return` forgot to
    finalize.
    """
    cursor_before: str | None = None
    cursor_floor: str | None = None
    cursor_scope: str | None = None
    scope: SyncScope | None = None
    as_of: str | None = None

    if args.source == "csv":
        if not args.path:
            exc: BaseException = ValueError("a CSV path is required with --source csv")
            print(f"ERROR {exc}", file=sys.stderr)
            _finalize_failed_run(store, run_id, RUN_FAILED, exc)
            return 2
        path = Path(args.path)
        if not path.exists():
            exc = FileNotFoundError(f"no such file: {path}")
            print(f"ERROR {exc}", file=sys.stderr)
            _finalize_failed_run(store, run_id, RUN_FAILED, exc)
            return 1
        artifacts.warn_if_exposed(path)
        try:
            records = SimplifiCsvSource(path).fetch()
        except (OSError, SchemaError, ValueError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            _finalize_failed_run(store, run_id, RUN_FAILED, exc)
            return 1
    else:
        from .sources.api_source import ApiError, SimplifiApiSource, client_from_env_or_age

        try:
            client = client_from_env_or_age(verbose=args.verbose)
            scope = api_scope(client, since=args.since)
            cursor_scope = scope.key()
            legacy_cursor = store.has_unscoped_cursor("api")
            stored_cursor = store.latest_cursor("api", cursor_scope)

            # A cursor can be a truthful statement about the provider and still
            # be wrong about what is on disk. Current rows are isolated by
            # source alone, so a complete rescan under another scope retires
            # this scope's rows; resuming from a mark earned before that
            # retirement would fetch only deltas and never restore them.
            snapshot_found, snapshot_scope = store.snapshot_owner_scope("api")
            foreign_snapshot = snapshot_found and snapshot_scope != cursor_scope
            reuse_blocker = scope.reuse_blocker()
            if stored_cursor is not None and (foreign_snapshot or reuse_blocker):
                stored_cursor = None

            cursor_before = None if args.full_rescan else args.modified_after or stored_cursor
            # A full rescan sends no modifiedAfter, but the watermark it would
            # be replacing still exists and must not be walked backwards. An
            # explicit --modified-after is a deliberate rewind, so it becomes
            # the floor itself rather than being vetoed by the stored value.
            cursor_floor = cursor_before or stored_cursor
            detail = (
                f"api since={args.since or 'all'} mode={mode} "
                f"modified_after={cursor_before or 'all'} scope={cursor_scope}"
            )
            store.record_run_scope(run_id, cursor_before, cursor_scope, detail)
            store.commit()

            api_source = SimplifiApiSource(
                client, date_on_after=args.since, modified_after=cursor_before
            )
            records = api_source.fetch()
            as_of = api_source.as_of
        except (SecretsError, ApiError) as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            _finalize_failed_run(store, run_id, RUN_FAILED, exc)
            return 1

        # Every one of these claims a full window was read, so all of them are
        # gated on cursor_before actually being None. An explicit
        # --modified-after starts where the operator said to start, and telling
        # them older records were recovered when they were not is worse than
        # saying nothing.
        if cursor_before is None and not args.full_rescan:
            if reuse_blocker:
                print(f"WARNING {reuse_blocker}.", file=sys.stderr)
            if foreign_snapshot:
                print(
                    "WARNING this database's current API rows were last replaced by a "
                    "complete rescan under a different cursor scope, so this scope's "
                    "cursor no longer describes what is stored. This run re-reads its "
                    "full window rather than resuming past rows that are not there.",
                    file=sys.stderr,
                )
            elif legacy_cursor and not stored_cursor:
                print(
                    "WARNING this database holds a cursor from before cursor scoping, which "
                    "cannot be attributed to a profile/dataset/query scope after the fact. "
                    "This run re-reads its full window once and earns a scoped cursor; "
                    "later runs are incremental again.",
                    file=sys.stderr,
                )

    cursor_after: str | None = None
    cursor_warning: str | None = None
    if args.source == "api":
        cursor_after, cursor_warning = _next_cursor(as_of, cursor_floor)

    complete_snapshot = _is_complete_snapshot(args)
    # No local guard here: persistence failures, and anything else raised in
    # this block, propagate to the caller's `_run_finalized`, which rolls the
    # work back and records the run as failed with its cause.
    outcomes = Counter(store.upsert_version(run_id, record) for record in records)
    retired_absent = 0
    if complete_snapshot:
        retired_absent = store.retire_absent_snapshot(
            run_id, {r["transaction_id"] for r in records}
        )
    store.record_accounts(
        {r["account_name"] for r in records if not r.get("is_deleted") and r["account_name"]}
    )
    store.finish_run(
        run_id,
        RUN_SUCCEEDED,
        len(records),
        cursor_after=cursor_after,
        complete_snapshot=complete_snapshot,
    )
    store.commit()

    print(f"INFO run {run_id}: versions {dict(outcomes)}")
    # Reported separately because they are different claims. A tombstone is the
    # provider saying a transaction was deleted; an absence is our inference
    # from a scan we believed complete. A surprising number of the second is
    # the signal that a truncated response was mistaken for a full one.
    tombstoned = outcomes.get("deleted", 0)
    if tombstoned or retired_absent:
        print(
            f"INFO retired {tombstoned} by provider tombstone, "
            f"{retired_absent} absent from a complete scan"
        )
    if args.source == "api":
        # Both halves of the exchange plus the identity they belong to, so a
        # surprising incremental window can be diagnosed from the log alone
        # rather than by re-deriving it.
        if scope is not None:
            print(f"INFO api cursor scope: {scope.describe()}")
        print(
            f"INFO api cursor: requested={cursor_before or 'none (full scan)'} "
            f"asOf={as_of or 'none'} recorded={cursor_after or 'unchanged'}"
        )
        if cursor_warning:
            print(f"WARNING {cursor_warning}", file=sys.stderr)
    _print_summary(records, args.source)
    print(f"INFO wrote {args.db}")
    return 0


def _no_successful_run_error(db: Path) -> str:
    """Explain the absence of a usable run in terms of what to do about it.

    "No successful run" is the same sentence whether nothing has been ingested,
    the last attempt failed with a recorded cause, or a run is still in flight —
    and those call for three different next steps. The lifecycle knows which it
    is, so it should say.
    """
    store = Store(db)
    try:
        latest = store.latest_run_summary()
    finally:
        store.close()
    if latest is None:
        return f"no runs recorded in {db}; run `ingest` first"
    state = str(latest["state"])
    if state == RUN_FAILED:
        reason = latest["error_message"] or "no cause was recorded"
        return (
            f"the most recent run ({latest['id']}, source {latest['source']!r}) failed and "
            f"no earlier successful run is available: {reason}. Analysis refuses an "
            "unsuccessful run rather than reporting on partial data"
        )
    if state == RUN_ABORTED:
        return (
            f"the most recent run ({latest['id']}, source {latest['source']!r}) was "
            "interrupted before it finished, and no earlier successful run is available. "
            "Re-run `ingest`"
        )
    if state == RUN_STARTED:
        return (
            f"the most recent run ({latest['id']}, source {latest['source']!r}) has not "
            "finished. If no ingest is in progress, that run's process died before it "
            "could record an outcome; re-run `ingest`"
        )
    return f"no successful run in {db}; run `ingest` first"


def _analysis_rows(args: argparse.Namespace) -> tuple[list[dict], int, str]:
    db = Path(args.db)
    if not db.exists():
        raise ValueError(f"no database at {db}; run `ingest` first")
    artifacts.harden_existing(db)
    run_id, source = _latest_run(db)
    if not run_id:
        # Only a succeeded run may become analysis input. Falling through would
        # select rows by source alone and report on whatever a failed or
        # half-finished run happened to leave behind.
        raise ValueError(_no_successful_run_error(db))
    rows = _rows(db, source)
    if not rows:
        raise ValueError(
            f"no current transactions for source {source!r} in {db}; run `ingest` first"
        )
    return rows, run_id, source


def _memory_proposals(
    rows: list[dict[str, Any]],
) -> tuple[MerchantMemory, list[tuple[dict[str, Any], Proposal | None]]]:
    memory = MerchantMemory()
    memory.train(rows)
    pending = [
        (row, memory.propose(row))
        for row in rows
        if row["is_uncategorized"] and is_statistics_eligible(row)
    ]
    pending.sort(key=lambda pair: (pair[1] is None, -abs(pair[0]["amount_minor_units"])))
    return memory, pending


def _as_of_rows(rows: list[dict], today: date) -> list[dict]:
    """Keep settled facts through today while retaining projection evidence."""
    cutoff = today.isoformat()
    return [row for row in rows if is_projected(row) or row["posted_on"] <= cutoff]


def _analysis_limitations(source: str, rows: list[dict]) -> list[str]:
    limitations: list[str] = []
    capabilities = SOURCE_CAPABILITIES.get(source)
    review_visible = [
        row for row in rows if row.get("review_eligible", assess_eligibility(row).eligible)
    ]
    unknown_states = sum(
        "unsupported_state" in (row.get("eligibility_reason_codes") or "") for row in review_visible
    )
    missing_states = sum(
        "missing_optional_field" in (row.get("eligibility_reason_codes") or "")
        for row in review_visible
    )
    if capabilities and not capabilities.settlement_state:
        limitations.append(
            f"{source.upper()} has no settlement or projection metadata; "
            f"{missing_states:,} eligible row(s) remain visible for general review, but "
            "settled-only analyses (memory, prioritization, staleness, recurring "
            "charges, and model examples) require explicit CLEARED state."
        )
    elif unknown_states or missing_states:
        limitations.append(
            f"{unknown_states + missing_states:,} eligible row(s) lack a confirmed "
            "CLEARED state and are excluded from settled-only analyses."
        )
    unknown_exclusions = sum(row.get("exclusion_flag") == 2 for row in rows)
    if capabilities and not capabilities.report_exclusion and unknown_exclusions:
        limitations.append(
            "The API bulk transaction response did not expose isExcludedFromReports; "
            f"{unknown_exclusions:,} row(s) remain review-visible with an unknown "
            "report-exclusion state. The result is not evidence of a clean review."
        )
    return limitations


def _known_categories(rows: list[dict]) -> set[str]:
    """Category labels this dataset already uses.

    Deliberately broader than :func:`_model_taxonomy`: settlement state governs
    what may train statistics, not whether a category label exists. Account
    names are removed so a proposal cannot relabel a transfer as its
    destination account.
    """
    accounts = {row["account_name"] for row in rows}
    return {
        (row["category"] or "").strip()
        for row in rows
        if (row["category"] or "").strip() and not row["is_uncategorized"]
    } - accounts


def _model_taxonomy(rows: list[dict]) -> list[str]:
    accounts = {row["account_name"] for row in rows}
    return sorted(
        {
            (row["category"] or "").strip()
            for row in rows
            if (
                (row["category"] or "").strip()
                and not row["is_uncategorized"]
                and is_statistics_eligible(row)
            )
        }
        - accounts
    )


def _curated_examples(
    prioritized: list[Any],
    subscription_findings: list[Any],
    proposals: list[tuple[dict[str, Any], Any]],
) -> list[dict[str, str]]:
    curated = judgment_examples.load_curated_examples()
    context = judgment_examples.context_from_review(prioritized, subscription_findings, proposals)
    return judgment_examples.select_relevant_examples(curated, context)


def cmd_analyze(args: argparse.Namespace) -> int:
    try:
        _prepare_paths(args)
    except artifacts.ArtifactError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    try:
        rows, run_id, source = _analysis_rows(args)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    today = _parse_today(args.today) or date.today()
    analysis_rows = _as_of_rows(rows, today)
    memory, proposals = _memory_proposals(analysis_rows)
    prioritized = prioritize.analyse(analysis_rows, today)
    staleness = prioritize.activity_staleness(analysis_rows, today)
    findings = subscriptions.analyse(analysis_rows, today)
    limitations = _analysis_limitations(source, analysis_rows)
    try:
        examples = _curated_examples(prioritized, findings, proposals)
    except judgment_examples.JudgmentExampleError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    out = Path(args.out)
    packet_path = Path(args.packet_out) if args.packet_out else out.with_name("review-packet.json")
    if out.resolve() == packet_path.resolve():
        print("ERROR --out and --packet-out must name different files", file=sys.stderr)
        return 2
    if Path(args.db).resolve() == packet_path.resolve():
        print("ERROR --packet-out and --db must name different files", file=sys.stderr)
        return 2
    artifacts.ensure_parent(out)
    packet = review_packet.build_packet(
        run_id=run_id,
        source=source,
        analysis_date=today,
        rows=analysis_rows,
        prioritized=prioritized,
        proposals=proposals,
        subscription_findings=findings,
        stale_account_count=sum(s["status"] == "stale" for s in staleness),
        limitations=limitations,
        examples=examples,
    )
    review_packet.write_packet(packet, packet_path)
    artifacts.secure_write_text(
        out,
        report.render(
            run_id=run_id,
            source=source,
            analysis_date=today,
            rows=analysis_rows,
            prioritized=prioritized,
            staleness=staleness,
            proposals=proposals,
            memory_stats=memory.stats(),
            subscription_findings=findings,
            limitations=limitations,
        ),
    )

    signals = Counter(signal.name for item in prioritized for signal in item.signals)
    resolved = sum(1 for _, proposal in proposals if proposal)
    print(f"INFO analysed {len(analysis_rows)} transactions")
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
    print(f"INFO wrote {packet_path}")
    return 0


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"no {label} at {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} at {path} is not valid JSON: {exc}") from exc


def _packet_binding_error(
    packet: dict, rows: list[dict], latest_run_id: int, latest_source: str
) -> str | None:
    """Confirm the packet was produced from this database, not another one.

    A run ID alone is not identity: two databases both sitting on run 1 would
    otherwise accept each other's packets, appending an immutable decision for
    a transaction this database has never seen. The dataset hash is recomputed
    from the selected database and must match the packet's.
    """
    run = packet["run"]
    if str(run.get("source")) != latest_source:
        return (
            f"review packet was produced from source {run.get('source')!r}, but the latest "
            f"successful run in this database used {latest_source!r}"
        )
    if run.get("run_id") != latest_run_id:
        # A superseded run is reported as a stale reference during validation.
        return None
    try:
        analysis_date = date.fromisoformat(str(run.get("analysis_date")))
    except ValueError:
        return f"review packet has an unusable analysis_date: {run.get('analysis_date')!r}"
    expected = review_packet.dataset_hash(_as_of_rows(rows, analysis_date))
    found = str(packet["source"].get("dataset_hash"))
    if expected != found:
        return (
            "review packet does not describe this database: expected dataset_hash "
            f"{expected[:12]}…, found {found[:12]}…. Re-run `analyze` against "
            "this database and review the packet it writes"
        )
    return None


def cmd_decide(args: argparse.Namespace) -> int:
    """Validate agent proposals and append immutable decision records."""
    try:
        _prepare_paths(args)
    except artifacts.ArtifactError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    db = Path(args.db)
    packet_path = Path(args.packet)
    proposals_path = Path(args.proposals)
    out = Path(args.out)
    for label, other in (("--db", db), ("--packet", packet_path), ("--proposals", proposals_path)):
        if out.resolve() == other.resolve():
            print(f"ERROR --out and {label} must name different files", file=sys.stderr)
            return 2
    if out.is_dir():
        print(f"ERROR --out must name a file, not a directory: {out}", file=sys.stderr)
        return 2

    # The packet and the proposals are artifacts of this workflow, not foreign
    # inputs like an exported CSV, so they get the same treatment as the files
    # we write. A group-writable proposals file is worth failing over rather
    # than warning about: another local user could edit the judgments in the
    # window between an agent producing them and `decide` recording them
    # immutably.
    try:
        for path in (packet_path, proposals_path):
            artifacts.harden_existing(path)
    except artifacts.ArtifactError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    try:
        packet = _load_json(packet_path, "review packet")
        document = _load_json(proposals_path, "proposals file")
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    try:
        review_packet.validate_packet(packet)
    except review_packet.PacketValidationError as exc:
        print(f"ERROR review packet at {packet_path} is invalid: {exc}", file=sys.stderr)
        return 1
    if not db.exists():
        print(f"ERROR no database at {db}; run `ingest` first", file=sys.stderr)
        return 1

    latest_run_id, latest_source = _latest_run(db)
    rows = _rows(db, latest_source)
    binding = _packet_binding_error(packet, rows, latest_run_id, latest_source)
    if binding:
        print(f"ERROR {binding}; no decision was recorded", file=sys.stderr)
        return 1
    retirement_store = Store(db)
    try:
        retired_ids = retirement_store.retired_transaction_ids(latest_source)
    finally:
        retirement_store.close()
    try:
        validated = decisions.validate_proposals(
            document,
            packet,
            allowed_categories=_known_categories(rows),
            latest_run_id=latest_run_id,
            retired_transaction_ids=retired_ids,
        )
    except decisions.ProposalValidationError as exc:
        print(
            f"ERROR rejected {len(exc.errors)} problem(s) in {proposals_path}; "
            "no decision was recorded",
            file=sys.stderr,
        )
        for error in exc.errors:
            print(f"ERROR {error}", file=sys.stderr)
        return 1

    reviewer = document["reviewer"]
    records = decisions.build_decision_records(validated, packet, reviewer)
    store = Store(db)
    staged: Path | None = None
    try:
        # One atomic step: take the write lock, re-confirm the run has not been
        # superseded, append, and prove the artifact is writable before commit.
        store.begin_immediate()
        if store.latest_successful_run() != (latest_run_id, latest_source):
            print(
                f"ERROR run {latest_run_id} was superseded by a concurrent ingest; "
                "no decision was recorded. Re-run `analyze` and review the new packet",
                file=sys.stderr,
            )
            store.rollback()
            return 1
        appended = store.append_decision_records(records)
        stored = store.stored_decisions([record["decision_id"] for record in records])
        staged = decisions.stage_decisions(
            decisions.build_decision_document(packet, reviewer, stored, appended_count=appended),
            out,
        )
        store.commit()
    except BaseException:
        store.rollback()
        if staged is not None:
            staged.unlink(missing_ok=True)
        raise
    finally:
        store.close()
    decisions.publish_decisions(staged, out)

    verdicts = Counter(record["decision"] for record in stored)
    print(f"INFO validated {len(records)} proposal(s) against run {latest_run_id}")
    print(f"INFO decisions: {dict(sorted(verdicts.items()))}")
    print(
        f"INFO appended {appended} decision record(s) to {db}; "
        f"{len(records) - appended} already recorded"
    )
    print(f"INFO wrote {out}; no provider state was changed")
    return 0


def cmd_subs(args: argparse.Namespace) -> int:
    try:
        _prepare_paths(args)
    except artifacts.ArtifactError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    try:
        rows, _, _ = _analysis_rows(args)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    today = _parse_today(args.today) or date.today()
    print(subscriptions.summary(_as_of_rows(rows, today), today))
    if not any((row.get("txn_state") or "") for row in rows):
        print(
            "\nWARNING source has no settlement/projection state; projected rows cannot be excluded.",
            file=sys.stderr,
        )
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    try:
        _prepare_paths(args)
    except artifacts.ArtifactError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    try:
        rows, _, source = _analysis_rows(args)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    limitations = _analysis_limitations(source, rows)
    for limitation in limitations:
        print(f"WARNING {limitation}", file=sys.stderr)

    memory = MerchantMemory()
    memory.train(rows)
    residue = [
        row
        for row in rows
        if (row["is_uncategorized"] and is_statistics_eligible(row) and memory.propose(row) is None)
    ]
    if not residue:
        print("INFO nothing left for a model to propose")
        return 0

    taxonomy = _model_taxonomy(rows)
    try:
        examples = _curated_examples([], [], [(row, None) for row in residue])
    except judgment_examples.JudgmentExampleError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    try:
        if not args.dry_run:
            _ensure_model_key(args.model, args.verbose)
        backend_cls = llm.BACKENDS[args.model]
        backend = backend_cls()
    except (SecretsError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
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
        artifacts.secure_write_text(out, "\n\n===== NEXT REQUEST =====\n\n".join(prompts))
        print(f"INFO dry run: {len(prompts)} request(s); wrote {out}")
        return 0

    out = Path(args.out)
    rows_by_id = {row["transaction_id"]: row for row in residue}
    with artifacts.secure_open(out, "w", newline="", encoding="utf-8") as fh:
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
                "source",
                "source_hash",
                "transaction_version_id",
                "run_id",
                "algorithm_version",
                "ruleset_version",
                "prompt_version",
                "prompt_hash",
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
                    _csv_safe_text(row["payee_display"]),
                    f"{row['amount_minor_units'] / 100:.2f}",
                    _csv_safe_text(proposal.category or ""),
                    f"{proposal.confidence:.2f}",
                    _csv_safe_text(proposal.rationale),
                    proposal.model,
                    "",
                    row.get("source", source),
                    row.get("source_hash", ""),
                    row.get("id", ""),
                    row.get("run_id", ""),
                    row.get("algorithm_version", ""),
                    row.get("ruleset_version", ""),
                    proposal.prompt_version,
                    proposal.prompt_hash,
                ]
            )
    resolved = sum(bool(proposal.category) for proposal in proposals)
    print(f"INFO model {backend.id}: {resolved}/{len(residue)} proposals with categories")
    print(f"INFO tokens in/out: {usage.input_tokens}/{usage.output_tokens}")
    print(f"INFO wrote {out}; proposals are not written back to Simplifi")
    return 0


def _csv_safe_text(value: str) -> str:
    """Prevent spreadsheet applications from interpreting text as a formula."""
    text = str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


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
        # Built from the profile already in hand. Calling verify() a second time
        # would put a network round trip outside this guard, where a transient
        # failure becomes a traceback instead of the clean error and exit code
        # the command promises.
        scope = scope_from_profile(client, profile, since=args.since)
    except (ValueError, ApiError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    print(f"INFO authenticated: profile keys {sorted(profile)[:8]}")
    print(
        f"INFO dataset {client.dataset_id[:8]}... · {len(accounts)} accounts · "
        f"{len(logins)} connections"
    )
    # Which cursor history this token and dataset would read and write. Without
    # it, "why did that run refetch everything?" is unanswerable from outside.
    print(f"INFO cursor scope: {scope.describe()}")
    blocker = scope.reuse_blocker()
    if blocker:
        print(f"WARNING {blocker}.", file=sys.stderr)
    healthy = True
    for login in logins:
        for health in _aggregator_health(login):
            level = "WARNING" if health["issues"] else "INFO"
            suffix = f"; issues={', '.join(health['issues'])}" if health["issues"] else ""
            print(
                f"{level} {health['name']}: status={health['status']} "
                f"code={health.get('code') or 'none'} "
                f"detail={health.get('detail') or 'none'} "
                f"last_success={health.get('last_success', 'unknown')} "
                f"next_manual={health.get('next_manual', 'unknown')}{suffix}"
            )
            healthy = healthy and not health["issues"]
    return 0 if healthy else 1


def cmd_schema(args: argparse.Namespace) -> int:
    from .sources.api_source import ApiError, schema_report

    try:
        print(schema_report(_api_client(args)))
    except (ValueError, ApiError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    return 0


def _add_storage_args(parser: argparse.ArgumentParser) -> None:
    """Options every artifact-producing subcommand shares.

    Repeated per subcommand rather than declared on the top-level parser
    because argparse would then require them *before* the subcommand name,
    which is the opposite of where anyone types them.
    """
    parser.add_argument(
        "--data-dir",
        help=(
            f"directory holding the database, reports, and ledgers "
            f"(default: ${artifacts.DATA_DIR_ENV}, else "
            f"$XDG_DATA_HOME/{artifacts.DEFAULT_DIR_NAME})"
        ),
    )
    parser.add_argument(
        "--allow-unsafe-paths",
        action="store_true",
        help="downgrade location refusals to warnings; permissions are still enforced",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="load CSV or API transactions into SQLite")
    ingest.add_argument("path", nargs="?", help="CSV path when --source csv")
    ingest.add_argument("--source", choices=("csv", "api"), default="csv")
    ingest.add_argument("--since", help="API dateOnAfter value, YYYY-MM-DD")
    cursor_group = ingest.add_mutually_exclusive_group()
    cursor_group.add_argument("--modified-after", help="API modifiedAfter cursor/value")
    cursor_group.add_argument(
        "--full-rescan",
        action="store_true",
        help="API only: omit modifiedAfter and perform a recovery/full scan",
    )
    ingest.add_argument("--db", default="simplifi.sqlite")
    ingest.add_argument("--verbose", action="store_true")
    _add_storage_args(ingest)
    ingest.set_defaults(func=cmd_ingest)

    analyze = sub.add_parser("analyze", help="build an HTML read-only review")
    analyze.add_argument("--db", default="simplifi.sqlite")
    analyze.add_argument("--out", default="report.html")
    analyze.add_argument(
        "--packet-out",
        help="review-packet.json path (default: beside --out)",
    )
    analyze.add_argument("--today", help="analysis date, YYYY-MM-DD")
    _add_storage_args(analyze)
    analyze.set_defaults(func=cmd_analyze)

    classify = sub.add_parser("classify", help="propose categories for unresolved rows")
    classify.add_argument("--db", default="simplifi.sqlite")
    classify.add_argument("--out", default="proposals.csv")
    classify.add_argument("--model", choices=sorted(llm.BACKENDS), default="luna")
    classify.add_argument("--chunk-size", type=_positive_int, default=llm.CHUNK_SIZE)
    classify.add_argument("--verbose", action="store_true")
    classify.add_argument(
        "--dry-run", action="store_true", help="write prompts without calling an API"
    )
    _add_storage_args(classify)
    classify.set_defaults(func=cmd_classify)

    decide = sub.add_parser("decide", help="validate agent proposals and append decision records")
    decide.add_argument("--db", default="simplifi.sqlite")
    decide.add_argument(
        "--packet",
        default="review-packet.json",
        help="review packet the proposals were made against",
    )
    decide.add_argument(
        "--proposals",
        default="proposals.json",
        help="structured agent judgment to validate",
    )
    decide.add_argument(
        "--out",
        default="decisions.json",
        help="validated decision records (never the input packet)",
    )
    _add_storage_args(decide)
    decide.set_defaults(func=cmd_decide)

    subs = sub.add_parser("subs", help="review recurring charges")
    subs.add_argument("--db", default="simplifi.sqlite")
    subs.add_argument("--today", help="analysis date, YYYY-MM-DD")
    _add_storage_args(subs)
    subs.set_defaults(func=cmd_subs)

    probe = sub.add_parser("probe", help="verify API authentication and connection reads")
    probe.add_argument("--since", help="report the cursor scope for this dateOnAfter value")
    probe.add_argument("--verbose", action="store_true")
    probe.set_defaults(func=cmd_probe)

    schema = sub.add_parser("schema", help="inspect API response shapes and pagination")
    schema.add_argument("--verbose", action="store_true")
    schema.set_defaults(func=cmd_schema)
    return parser


def install_termination_handler() -> bool:
    """Make SIGTERM raise, so a terminated run can record that it was aborted.

    Ctrl-C already raises KeyboardInterrupt, but SIGTERM's default action ends
    the process outright — no exception, no unwinding, no chance to finalize.
    That is the ordinary way a service manager, a container stop, or a plain
    `kill` ends a scheduled run, so without this the lifecycle's promise that
    an interruption is recorded as `aborted` would hold only for the case
    someone was sitting at a keyboard. The run would instead sit at `started`
    forever, indistinguishable from one still in flight.

    Returns whether the handler was installed. Signal handlers can only be set
    from the main thread, and SIGTERM does not exist on every platform, so an
    embedding caller gets a no-op rather than an exception — losing the
    guarantee, not the command.
    """
    if not hasattr(signal, "SIGTERM"):
        return False
    try:
        signal.signal(signal.SIGTERM, _raise_system_exit)
    except ValueError:
        # Not the main thread. Nothing to do, and nothing worth failing over.
        return False
    return True


def _raise_system_exit(signum: int, _frame) -> None:
    # SystemExit rather than a bespoke exception: `_run_finalized` already
    # treats it as an interruption, and anything that does not know about the
    # lifecycle still sees an ordinary exit.
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    install_termination_handler()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, sqlite3.Error, ValueError, artifacts.ArtifactError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
