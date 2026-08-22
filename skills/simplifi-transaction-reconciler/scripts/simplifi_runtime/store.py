"""SQLite store with numbered migrations.

Append-only: a transaction that changes gets a new `transaction_version` row and
the previous one has `is_current` cleared. Nothing is ever updated in place.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import artifacts

ALGORITHM_VERSION = "0.1.0"
#: 0.3.0: merchant identity, account display and money derivation moved to
#: `evidence`, so the same source facts can now produce a different normalized
#: row than they did before. Re-versioned deliberately — `upsert_version`
#: compares the ruleset version alongside the content hash, so every stored row
#: is re-derived on the next ingest instead of keeping a value produced by
#: rules that no longer exist.
RULESET_VERSION = "0.3.0"

#: Run lifecycle. `started` is written when the row is created and is also what
#: a run left behind by a killed process keeps forever — we never learned what
#: happened to it, and inventing a conclusion would be worse than admitting
#: that. `aborted` is reserved for interruptions the process itself observed
#: (Ctrl-C, SIGTERM) and could record on the way out.
RUN_STARTED = "started"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_ABORTED = "aborted"

#: Only a terminal state may be written by `finish_run`.
TERMINAL_RUN_STATES = frozenset({RUN_SUCCEEDED, RUN_FAILED, RUN_ABORTED})

#: Legacy `outcome` mirror, derived from `state` so the two cannot drift.
#: Nothing reads `outcome`; it is written only so an operator's existing ad-hoc
#: query does not start returning nothing after an upgrade.
_LEGACY_OUTCOME = {
    RUN_SUCCEEDED: "success",
    RUN_FAILED: "failure",
    RUN_ABORTED: "failure",
}

#: Why a transaction stopped being current. The distinction is epistemic, not
#: cosmetic: a tombstone is the provider stating the transaction was deleted,
#: while an absence is our own inference from a scan we believed complete. A
#: truncated response mistaken for a complete one produces a wave of the second
#: kind that looks exactly like the first, so the reason has to be recorded at
#: the moment it is known rather than reconstructed later.
RETIRED_BY_TOMBSTONE = "provider_tombstone"
RETIRED_BY_ABSENCE = "absent_from_snapshot"

#: Column order for `decision_record` inserts. Kept here rather than imported
#: from `decisions` so the store stays free of validation dependencies.
DECISION_RECORD_COLUMNS = (
    "decision_id",
    "run_id",
    "source",
    "analysis_date",
    "transaction_id",
    "proposal_id",
    "proposal_hash",
    "dataset_hash",
    "decision",
    "action",
    "category",
    "rationale",
    "reviewer_kind",
    "reviewer_id",
    "recorded_at",
    "validator_version",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path, migrations_dir: Path | None = None):
        self.path = Path(path)
        # Create the file ourselves, at 0600, before SQLite gets the chance to
        # make it under the ambient umask. Every later sidecar (-wal, -shm)
        # inherits the main database's mode, so this one call covers them too.
        artifacts.create_private(self.path)
        self.migrations_dir = Path(migrations_dir or Path(__file__).with_name("migrations"))
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._run_sources: dict[int, str] = {}
        self._run_scopes: dict[int, str | None] = {}
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    # --- migrations ---------------------------------------------------------

    def _migrate(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {r["name"] for r in self.conn.execute("SELECT name FROM schema_migrations")}
        for sql_file in sorted(self.migrations_dir.glob("*.sql")):
            if sql_file.name in applied:
                continue
            script = sql_file.read_text(encoding="utf-8")
            name = sql_file.name.replace("'", "''")
            applied_at = _now().replace("'", "''")
            migration = (
                "BEGIN;\n"
                + script
                + "\nINSERT INTO schema_migrations (name, applied_at) VALUES ('"
                + name
                + "', '"
                + applied_at
                + "');\nCOMMIT;"
            )
            try:
                self.conn.executescript(migration)
            except sqlite3.Error:
                self.conn.rollback()
                raise

    # --- runs ---------------------------------------------------------------

    def start_run(self, source: str, source_detail: str, cursor_before: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, source, source_detail,"
            " algorithm_version, ruleset_version, cursor_before) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _now(),
                source,
                source_detail,
                ALGORITHM_VERSION,
                RULESET_VERSION,
                cursor_before,
            ),
        )
        if cur.lastrowid is None:
            raise sqlite3.DatabaseError("SQLite did not return a run ID")
        run_id = int(cur.lastrowid)
        self._run_sources[run_id] = source
        return run_id

    def finish_run(
        self,
        run_id: int,
        state: str,
        row_count: int,
        cursor_after: str | None = None,
        complete_snapshot: bool = False,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Move a run to a terminal state, recording why if it failed.

        Returns whether the transition happened. Terminal means terminal: a run
        that has already finished is left exactly as it is, and the caller is
        told nothing changed.

        That guard is load-bearing, not decorative. Anything raised *after* the
        ingest committed — a `BrokenPipeError` from printing a summary into a
        closed pipe is the everyday case — would otherwise reach the failure
        path and rewrite a succeeded run as failed. The rollback that
        accompanies it cannot take back the committed rows, so the result is a
        database whose transaction rows are current and whose run says it
        failed: analysis then rejects complete data, or attributes those rows
        to some earlier run. Refusing the transition keeps the two consistent
        however the code around it changes later.

        Rejects a non-terminal or unknown state rather than storing it. A typo
        like ``"success"`` would otherwise write a value no query matches, and
        the run would be invisible to analysis while looking finished in the
        table — the precise failure the lifecycle exists to prevent.
        """
        if state not in TERMINAL_RUN_STATES:
            raise ValueError(
                f"run state must be one of {sorted(TERMINAL_RUN_STATES)}, got {state!r}"
            )
        cur = self.conn.execute(
            "UPDATE runs SET finished_at = ?, state = ?, outcome = ?, row_count = ?, "
            "cursor_after = ?, complete_snapshot = ?, error_class = ?, error_message = ? "
            "WHERE id = ? AND state = ?",
            (
                _now(),
                state,
                _LEGACY_OUTCOME[state],
                row_count,
                cursor_after,
                int(complete_snapshot),
                error_class,
                error_message,
                run_id,
                RUN_STARTED,
            ),
        )
        return cur.rowcount > 0

    def record_run_scope(
        self,
        run_id: int,
        cursor_before: str | None,
        cursor_scope: str | None,
        source_detail: str | None = None,
    ) -> None:
        """Attach scope and cursor provenance discovered after the run started.

        The run row is opened before the API client exists, so that an auth or
        network failure is still recorded as a failed run rather than vanishing.
        The scope can only be resolved once that client is up, so it lands here
        rather than in :meth:`start_run`.
        """
        self._run_scopes[run_id] = cursor_scope
        if source_detail is None:
            self.conn.execute(
                "UPDATE runs SET cursor_before = ?, cursor_scope = ? WHERE id = ?",
                (cursor_before, cursor_scope, run_id),
            )
            return
        self.conn.execute(
            "UPDATE runs SET cursor_before = ?, cursor_scope = ?, source_detail = ? WHERE id = ?",
            (cursor_before, cursor_scope, source_detail, run_id),
        )

    def run_scope(self, run_id: int) -> tuple[str, str | None]:
        """The `(source, cursor scope)` a run writes under.

        Both halves identify the materialized state the run owns, so they are
        resolved together and cached together. Reading them separately invites
        a caller to filter current rows by source and forget the scope, which
        is exactly the bug this pairing exists to prevent.
        """
        if run_id in self._run_sources and run_id in self._run_scopes:
            return self._run_sources[run_id], self._run_scopes[run_id]
        row = self.conn.execute(
            "SELECT source, cursor_scope FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError(f"unknown run ID: {run_id}")
        source = str(row["source"])
        scope = row["cursor_scope"]
        scope = str(scope) if scope is not None else None
        self._run_sources[run_id] = source
        self._run_scopes[run_id] = scope
        return source, scope

    def latest_cursor(self, source: str, cursor_scope: str | None = None) -> str | None:
        """The newest earned cursor for exactly this source and scope.

        `IS` rather than `=` so an unscoped lookup matches the unscoped rows
        left by installations that predate cursor scoping — under `=` a NULL
        never matches anything, including itself, and every legacy cursor would
        be invisible even to the caller that owns it.

        A scope that has never been synchronized returns None. That is the
        correct answer, not a miss to paper over: this history has no
        high-water mark, so the run must read its window from the start rather
        than borrow a mark earned against different data.
        """
        row = self.conn.execute(
            "SELECT cursor_after FROM runs WHERE source = ? AND cursor_scope IS ? "
            "AND state = 'succeeded' AND cursor_after IS NOT NULL ORDER BY id DESC LIMIT 1",
            (source, cursor_scope),
        ).fetchone()
        return str(row["cursor_after"]) if row else None

    def has_unscoped_cursor(self, source: str) -> bool:
        """Whether an earned but unattributable cursor predates scoping.

        Used only to explain the one-time full window after upgrading, so the
        wider fetch reads as an expected migration step rather than a fault.
        """
        row = self.conn.execute(
            "SELECT 1 FROM runs WHERE source = ? AND cursor_scope IS NULL "
            "AND state = 'succeeded' AND cursor_after IS NOT NULL LIMIT 1",
            (source,),
        ).fetchone()
        return row is not None

    def latest_successful_run(self) -> tuple[int, str, str | None]:
        """The newest successful run as ``(id, source, cursor scope)``.

        The scope comes back with the source because every consumer needs both:
        current rows are attributed to a `(source, scope)` pair, and a caller
        that selected on source alone would read whatever other datasets share
        the database. ``(0, "unknown", None)`` when there is no successful run.
        """
        row = self.conn.execute(
            "SELECT id, source, cursor_scope FROM runs WHERE state = 'succeeded' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0, "unknown", None
        scope = row["cursor_scope"]
        return int(row["id"]), str(row["source"]), (str(scope) if scope is not None else None)

    def latest_run_summary(self) -> dict | None:
        """The newest run of any state, for explaining why analysis found none.

        "No successful run" is not an actionable message on its own. Whether
        the last attempt failed with a recorded error, is still running, or was
        interrupted changes what the operator should do next, so the caller
        needs the row rather than just its absence.
        """
        row = self.conn.execute(
            "SELECT id, source, state, error_class, error_message FROM runs "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    #: Everything `status` needs to say what a run was and how it ended.
    RUN_STATUS_COLUMNS = (
        "id, source, state, started_at, finished_at, row_count, "
        "cursor_before, cursor_after, cursor_scope, complete_snapshot, "
        "error_class, error_message"
    )

    def latest_run_per_schedule(self) -> list[dict]:
        """The newest run for each (source, cursor scope) pair.

        A schedule's identity is its source *and* its cursor scope, because
        that is what the cursor itself is keyed by: two API schedules over
        different profiles, datasets, tokens, or `--since` bounds keep separate
        histories by design. Grouping by source alone would let a later success
        for one of them bury a failure in the other, and `status` would report
        healthy while a synchronization had been dead for weeks — which is the
        exact silence these safeguards exist to break.
        """
        rows = self.conn.execute(
            f"SELECT {self.RUN_STATUS_COLUMNS} FROM runs WHERE id IN ("
            "  SELECT MAX(id) FROM runs GROUP BY source, IFNULL(cursor_scope, '')"
            ") ORDER BY source, IFNULL(cursor_scope, '')"
        )
        return [dict(row) for row in rows]

    def cursor_scopes(self, source: str) -> list[str]:
        """Distinct cursor scopes a source has succeeded under.

        Run history, not stored state: it answers "how many datasets has this
        database ever synchronized?", which is what decides whether legacy rows
        can be attributed to anyone. For what is materialized right now, ask
        :meth:`current_scopes`.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT cursor_scope FROM runs "
            "WHERE source = ? AND state = ? AND cursor_scope IS NOT NULL "
            "ORDER BY cursor_scope",
            (source, RUN_SUCCEEDED),
        )
        return [row["cursor_scope"] for row in rows]

    def run_history(self, limit: int = 10, source: str | None = None) -> list[dict]:
        """Recent runs, newest first — the trail behind the current state."""
        if source:
            rows = self.conn.execute(
                f"SELECT {self.RUN_STATUS_COLUMNS} FROM runs WHERE source = ? "
                "ORDER BY id DESC LIMIT ?",
                (source, int(limit)),
            )
        else:
            rows = self.conn.execute(
                f"SELECT {self.RUN_STATUS_COLUMNS} FROM runs ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
        return [dict(row) for row in rows]

    def run_by_id(self, run_id: int) -> dict | None:
        row = self.conn.execute(
            f"SELECT {self.RUN_STATUS_COLUMNS} FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def begin_immediate(self) -> None:
        """Take the write lock now, so a concurrent ingest cannot slip in later.

        Reading the latest run and appending decisions must be one atomic step;
        otherwise an ingest committing between them makes the runtime record
        exactly the stale judgment the contract says must fail closed.
        """
        self.conn.execute("BEGIN IMMEDIATE")

    def _retire_version(
        self,
        run_id: int,
        source: str,
        version_id: int,
        transaction_id: str,
        reason: str,
        cursor_scope: str | None = None,
    ) -> None:
        """Clear `is_current` and append the evidence that it was cleared.

        The two halves belong together. Clearing the flag on its own leaves a
        row that is no longer current with nothing to say why, which run did
        it, or on what grounds — the row survives and the event does not.
        """
        self.conn.execute(
            "UPDATE transaction_version SET is_current = 0 WHERE id = ?", (version_id,)
        )
        self.conn.execute(
            "INSERT INTO retirement_record (transaction_id, source, cursor_scope,"
            " prior_version_id, run_id, reason, retired_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (transaction_id, source, cursor_scope, version_id, run_id, reason, _now()),
        )

    def retire_absent_snapshot(self, run_id: int, observed_ids: set[str]) -> int:
        """Retire current rows absent from a complete replacement snapshot.

        Recorded as an inference (`absent_from_snapshot`), never as a deletion.
        The provider said nothing about these transactions; we concluded they
        were gone because a scan we believed complete did not mention them. If
        that belief was wrong the records here are what makes it recoverable.

        Only the run's own scope is considered. A complete rescan of one dataset
        is silent about every other dataset in the database, and retiring rows
        it never asked about would record an inference no scan supports.
        """
        source, cursor_scope = self.run_scope(run_id)
        current = self.conn.execute(
            "SELECT id, transaction_id FROM transaction_version "
            "WHERE source = ? AND cursor_scope IS ? AND is_current = 1",
            (source, cursor_scope),
        ).fetchall()
        retired = 0
        for row in current:
            if row["transaction_id"] not in observed_ids:
                self._retire_version(
                    run_id,
                    source,
                    int(row["id"]),
                    str(row["transaction_id"]),
                    RETIRED_BY_ABSENCE,
                    cursor_scope,
                )
                retired += 1
        return retired

    def retirements(
        self,
        source: str | None = None,
        transaction_id: str | None = None,
        cursor_scope: str | None = None,
    ) -> list[dict]:
        """Retirement history, oldest first. Never filtered by current state.

        A retired transaction is exactly the one whose story is hardest to read
        off the current view, so this deliberately does not join against it.
        """
        clauses, params = [], []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if transaction_id is not None:
            clauses.append("transaction_id = ?")
            params.append(transaction_id)
        if cursor_scope is not None:
            clauses.append("cursor_scope = ?")
            params.append(cursor_scope)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT id, transaction_id, source, cursor_scope, prior_version_id, run_id, "
                "reason, retired_at "
                f"FROM retirement_record{where} ORDER BY id",
                params,
            )
        ]

    def retired_transaction_ids(self, source: str, cursor_scope: str | None) -> set[str]:
        """Transactions currently retired in this scope: retired, not restored.

        A retirement is not permanent — a provider can resurrect a transaction,
        and a later run makes it current again. Reading the retirement table
        alone would treat those as still gone, so membership is confirmed
        against the absence of a current version *in this scope*.

        Legacy retirements (NULL scope, written before migration 015) match
        every scope. They cannot be attributed after the fact — the table is
        append-only by trigger, so there is no honest backfill — and the
        `NOT EXISTS` clause keeps the looseness harmless: a transaction that is
        current here is not reported retired here whatever the legacy row says.
        Where it is not current, naming it retired is the fail-closed answer,
        since the caller uses this set to reject decisions about rows that may
        no longer exist.

        `cursor_scope` has no default on purpose. A default of None would read
        as "any scope" at the call site and mean "the legacy scope" in the
        query, and a caller who omitted it would silently get an empty set —
        which this function's consumer treats as "nothing is retired".
        """
        return {
            str(row["transaction_id"])
            for row in self.conn.execute(
                "SELECT DISTINCT r.transaction_id FROM retirement_record r "
                "WHERE r.source = ? AND (r.cursor_scope IS ? OR r.cursor_scope IS NULL) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM transaction_version v WHERE v.transaction_id = r.transaction_id"
                "  AND v.source = r.source AND v.cursor_scope IS ? AND v.is_current = 1)",
                (source, cursor_scope, cursor_scope),
            )
        }

    def current_scopes(self, source: str) -> list[str | None]:
        """Scopes that currently hold materialized rows for this source.

        Distinct from :meth:`cursor_scopes`, which reads run history. A report
        covers one scope; this is how a caller finds out that the database
        holds others and can say so rather than quietly showing a slice.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT cursor_scope FROM transaction_version "
            "WHERE source = ? AND is_current = 1 ORDER BY IFNULL(cursor_scope, '')",
            (source,),
        )
        return [
            str(row["cursor_scope"]) if row["cursor_scope"] is not None else None for row in rows
        ]

    def adopt_legacy_scope(self, run_id: int, observed_ids: set[str]) -> int:
        """Claim pre-scoping rows this run's own fetch just proved are its own.

        Rows written before migration 015 carry NULL, which is a real scope
        holding everything the source materialized back when state was isolated
        by source alone. Left there they are current forever and invisible to
        every scoped reader — a slow leak plus a permanently stale view.

        Adoption is by evidence, one transaction at a time: a legacy row is
        claimed only when this run's complete rescan actually returned that
        transaction ID, which is the provider stating it belongs to this
        dataset. The first version claimed the whole bucket whenever no other
        scope had succeeded yet, which on a fresh upgrade meant the first scope
        to run adopted another dataset's rows outright — and because a normal
        incremental ingest is not a complete snapshot, absence retirement never
        cleaned them up and `analyze` reported the mixture as one clean scope.
        That was worse than the collision it replaced: it was silent.

        Only a complete snapshot may adopt, since only a complete snapshot has
        an observed-ID set that means "everything this dataset holds". Rows no
        rescan has claimed stay put; another scope's rescan may still claim
        them, and :meth:`retire_orphaned_legacy` clears whatever is left once
        every scope has been rebuilt.

        Retirement rows keep their NULL: that table is append-only by trigger,
        and :meth:`retired_transaction_ids` reads legacy rows across scopes for
        exactly this reason.
        """
        source, cursor_scope = self.run_scope(run_id)
        if cursor_scope is None or not observed_ids:
            return 0
        legacy = self.conn.execute(
            "SELECT id, transaction_id FROM transaction_version "
            "WHERE source = ? AND cursor_scope IS NULL AND is_current = 1",
            (source,),
        ).fetchall()
        claimed = [row["id"] for row in legacy if str(row["transaction_id"]) in observed_ids]
        for version_id in claimed:
            self.conn.execute(
                "UPDATE transaction_version SET cursor_scope = ? WHERE id = ?",
                (cursor_scope, version_id),
            )
        return len(claimed)

    def retire_orphaned_legacy(self, run_id: int) -> int:
        """Retire the legacy rows no scope's rescan ever claimed.

        The escape hatch for a database whose scopes have all been rebuilt: what
        is still sitting in the NULL bucket then belongs to no dataset the
        provider still serves, and without this it would warn forever.

        Deliberately explicit rather than automatic. "Every scope has been
        rescanned" is a fact only the operator knows — the runtime cannot tell a
        scope that no longer exists from one that simply has not run yet, and
        guessing wrong retires live history.
        """
        source, _ = self.run_scope(run_id)
        legacy = self.conn.execute(
            "SELECT id, transaction_id FROM transaction_version "
            "WHERE source = ? AND cursor_scope IS NULL AND is_current = 1",
            (source,),
        ).fetchall()
        for row in legacy:
            self._retire_version(
                run_id, source, int(row["id"]), str(row["transaction_id"]), RETIRED_BY_ABSENCE
            )
        return len(legacy)

    def latest_successful_run_in_scope(
        self, source: str, cursor_scope: str | None
    ) -> tuple[int, str, str | None]:
        """The newest successful run for one `(source, scope)` pair.

        Supersession is a per-scope question now that state is per-scope. Asking
        it database-wide would let an unrelated dataset's ingest invalidate a
        packet it provably did not touch.
        """
        row = self.conn.execute(
            "SELECT id FROM runs WHERE state = 'succeeded' AND source = ? "
            "AND cursor_scope IS ? ORDER BY id DESC LIMIT 1",
            (source, cursor_scope),
        ).fetchone()
        return (int(row["id"]), source, cursor_scope) if row else (0, source, cursor_scope)

    def unattributed_row_count(self, source: str) -> int:
        """Current rows still in the legacy scope, which adoption declined."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transaction_version "
            "WHERE source = ? AND cursor_scope IS NULL AND is_current = 1",
            (source,),
        ).fetchone()
        return int(row["n"])

    def rollback(self) -> None:
        self.conn.rollback()

    # --- transactions -------------------------------------------------------

    @staticmethod
    def source_hash(record: dict) -> str:
        fields = (
            "posted_on",
            "transacted_on",
            "account_name",
            "account_id",
            "amount_minor_units",
            "currency",
            "payee_raw",
            "category",
            "exclusion_flag",
            "recurring_flag",
            "txn_state",
            "match_state",
            "scheduled_model_id",
            "scheduled_due_on",
            "is_split",
            "is_reviewed",
            "inferred_category",
            "excluded_from_f2s",
        )
        payload = json.dumps({k: record.get(k) for k in fields}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def upsert_version(self, run_id: int, record: dict) -> str:
        """Append a version if the content hash changed. Returns 'new'|'changed'|'same'.

        Identity is `(transaction_id, source, cursor scope)`. Provider IDs are
        unique within a dataset, not across them, and even where they collide
        by accident the two rows describe different accounts — so the scope is
        part of what makes a transaction the same transaction.
        """
        source, cursor_scope = self.run_scope(run_id)

        txid = record["transaction_id"]
        if record.get("is_deleted"):
            # Select before updating: the retirement record names the exact
            # version it retired, and a blind UPDATE discards the identity that
            # makes a repeated retire/reappear/retire cycle readable afterwards.
            current = self.conn.execute(
                "SELECT id FROM transaction_version "
                "WHERE transaction_id = ? AND source = ? AND cursor_scope IS ? "
                "AND is_current = 1",
                (txid, source, cursor_scope),
            ).fetchall()
            for row in current:
                self._retire_version(
                    run_id, source, int(row["id"]), str(txid), RETIRED_BY_TOMBSTONE, cursor_scope
                )
            return "deleted" if current else "deleted_missing"

        shash = self.source_hash(record)
        row = self.conn.execute(
            "SELECT id, source_hash, algorithm_version, ruleset_version "
            "FROM transaction_version WHERE transaction_id = ? AND source = ? "
            "AND cursor_scope IS ? AND is_current = 1",
            (txid, source, cursor_scope),
        ).fetchone()

        if (
            row
            and row["source_hash"] == shash
            and row["algorithm_version"] == ALGORITHM_VERSION
            and row["ruleset_version"] == RULESET_VERSION
        ):
            return "same"

        if row:
            self.conn.execute(
                "UPDATE transaction_version SET is_current = 0 WHERE id = ?", (row["id"],)
            )

        cols = [
            "transaction_id",
            "run_id",
            "observed_at",
            "source_hash",
            "is_current",
            "source",
            "cursor_scope",
            "algorithm_version",
            "ruleset_version",
            "posted_on",
            "transacted_on",
            "account_name",
            "account_name_known",
            "account_id",
            "amount_minor_units",
            "currency",
            "currency_exponent",
            "payee_raw",
            "payee_normalized",
            "payee_canonical",
            "payee_display",
            "norm_rules_applied",
            "original_currency",
            "original_amount",
            "is_foreign_charge",
            "category",
            "inferred_category",
            "is_uncategorized",
            "exclusion_flag",
            "excluded_from_f2s",
            "recurring_flag",
            "is_split",
            "is_reviewed",
            "kind",
            "poisons_statistics",
            "semantics_reasons",
            "txn_state",
            "match_state",
            "scheduled_model_id",
            "scheduled_due_on",
            "review_eligible",
            "eligibility_reason_codes",
        ]
        values = {
            **record,
            "run_id": run_id,
            "observed_at": _now(),
            "source_hash": shash,
            "is_current": 1,
            "source": source,
            "cursor_scope": cursor_scope,
            "algorithm_version": ALGORITHM_VERSION,
            "ruleset_version": RULESET_VERSION,
        }
        for column in ("excluded_from_f2s", "is_split", "is_reviewed"):
            values.setdefault(column, 0)
        values.setdefault("account_name_known", int(bool(record.get("account_name"))))
        values.setdefault("review_eligible", 1)
        values.setdefault("eligibility_reason_codes", "")
        self.conn.execute(
            f"INSERT INTO transaction_version ({','.join(cols)})"
            f" VALUES ({','.join('?' * len(cols))})",
            [values.get(c) for c in cols],
        )
        return "changed" if row else "new"

    # --- decisions ----------------------------------------------------------

    def append_decision_records(self, records: list[dict]) -> int:
        """Append validated decisions and return how many were new.

        Decision IDs are content-derived, so re-recording an identical decision
        is a no-op rather than a duplicate. A changed judgment arrives with a
        different hash and is appended beside the original; the table's triggers
        reject any attempt to edit or remove what is already there.
        """
        appended = 0
        for record in records:
            cursor = self.conn.execute(
                f"INSERT INTO decision_record ({','.join(DECISION_RECORD_COLUMNS)})"
                f" VALUES ({','.join('?' * len(DECISION_RECORD_COLUMNS))})"
                " ON CONFLICT(decision_id) DO NOTHING",
                [record.get(column) for column in DECISION_RECORD_COLUMNS],
            )
            appended += max(cursor.rowcount, 0)
        return appended

    def stored_decisions(self, decision_ids: list[str]) -> list[dict]:
        """Return the stored form of the given decisions, in the order requested.

        Callers export what the database actually holds rather than the
        candidate they just built, so an already-recorded decision reports its
        original timestamp instead of the current clock.
        """
        columns = ",".join(DECISION_RECORD_COLUMNS)
        found = {}
        for decision_id in decision_ids:
            row = self.conn.execute(
                f"SELECT {columns} FROM decision_record WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if row is not None:
                found[decision_id] = dict(row)
        return [found[decision_id] for decision_id in decision_ids if decision_id in found]

    def decision_records(self, run_id: int | None = None) -> list[dict]:
        """Return stored decisions in append order, newest last."""
        if run_id is None:
            rows = self.conn.execute("SELECT * FROM decision_record ORDER BY id")
        else:
            rows = self.conn.execute(
                "SELECT * FROM decision_record WHERE run_id = ? ORDER BY id", (run_id,)
            )
        return [dict(row) for row in rows]

    # --- mutations ----------------------------------------------------------

    def record_mutation_attempt(
        self,
        *,
        attempt_id: str,
        capability: str,
        transaction_id: str,
        decision_id: str | None,
        run_id: int | None,
        source: str,
        source_hash: str,
        authorization,
        before_document,
        after_document,
        change_summary: str,
        undoes_attempt_id: str | None = None,
    ) -> None:
        """Record an intended write. Committed *before* the request leaves.

        The documents are stored whole, as JSON text, exactly as they were read
        and as they will be sent. A field-level diff would be smaller and would
        also be useless for an undo, which has to reconstruct a document.
        """
        self.conn.execute(
            "INSERT INTO mutation_attempt (attempt_id, capability, transaction_id,"
            " decision_id, run_id, source, source_hash, authorized_by, authorization_note,"
            " authorized_at, before_document, after_document, change_summary, attempted_at,"
            " undoes_attempt_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                capability,
                transaction_id,
                decision_id,
                run_id,
                source,
                source_hash,
                authorization.authorized_by,
                authorization.note,
                authorization.authorized_at,
                json.dumps(before_document, sort_keys=True),
                json.dumps(after_document, sort_keys=True),
                change_summary,
                _now(),
                undoes_attempt_id,
            ),
        )

    def record_mutation_outcome(
        self,
        *,
        attempt_id: str,
        outcome: str,
        job_id: str = "",
        job_status: str = "",
        error_class: str = "",
        error_message: str = "",
    ) -> None:
        """Record what the provider did. An attempt with no outcome never settled."""
        self.conn.execute(
            "INSERT INTO mutation_outcome (attempt_id, outcome, job_id, job_status,"
            " error_class, error_message, settled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                outcome,
                job_id or None,
                job_status or None,
                error_class or None,
                error_message or None,
                _now(),
            ),
        )

    def mutation_attempt(self, attempt_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM mutation_attempt WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def mutation_outcome(self, attempt_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM mutation_outcome WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def undo_of(self, attempt_id: str) -> dict | None:
        """The attempt that reversed this one, if any — settled or not.

        The newest one, and deliberately not filtered by outcome: an undo that
        left and never settled may have landed, so the caller decides. Newest
        rather than first because a rejected undo may be followed by a
        successful retry, and it is the retry that must block a third.
        """
        row = self.conn.execute(
            "SELECT * FROM mutation_attempt WHERE undoes_attempt_id = ? ORDER BY id DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def applied_decision_ids(self) -> set[str]:
        """Decisions already carried out, so a rerun does not write them twice.

        An attempt with no outcome counts as applied. It may have landed, and
        the safe reading of "we do not know" is not "do it again".

        **Before the write path is unblocked, this needs a database constraint
        behind it.** Read here and acted on later, it is a check-then-write:
        two overlapping processes can both see a decision unapplied. Today the
        caller re-reads under `begin_immediate()` and SQLite's write lock
        serializes them, so the guarantee is the lock's rather than the
        schema's. The structural fix is a partial unique index —
        `CREATE UNIQUE INDEX … ON mutation_attempt (decision_id)
        WHERE undoes_attempt_id IS NULL` — and it belongs with a test that
        genuinely runs two concurrent applies, which is why it is not here yet.
        The mutation register records it alongside the other write-path
        preconditions so it is not found only by reading this docstring.
        """
        return {
            str(row["decision_id"])
            for row in self.conn.execute(
                "SELECT DISTINCT a.decision_id FROM mutation_attempt a "
                "LEFT JOIN mutation_outcome o ON o.attempt_id = a.attempt_id "
                "WHERE a.decision_id IS NOT NULL AND a.undoes_attempt_id IS NULL "
                "AND (o.outcome IS NULL OR o.outcome != 'failed')"
            )
        }

    def mutation_history(self, limit: int = 20) -> list[dict]:
        """Attempts newest first, each with its outcome where one exists."""
        rows = self.conn.execute(
            "SELECT a.*, o.outcome, o.job_id, o.job_status, o.error_class, o.error_message,"
            " o.settled_at FROM mutation_attempt a"
            " LEFT JOIN mutation_outcome o ON o.attempt_id = a.attempt_id"
            " ORDER BY a.id DESC LIMIT ?",
            (int(limit),),
        )
        return [dict(row) for row in rows]

    def record_accounts(self, names: set[str]) -> None:
        for name in sorted(names):
            self.conn.execute(
                "INSERT INTO accounts (name, first_seen, last_seen) VALUES (?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET last_seen = excluded.last_seen",
                (name, _now(), _now()),
            )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
