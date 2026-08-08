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

ALGORITHM_VERSION = "0.1.0"
RULESET_VERSION = "0.2.0"

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrations_dir = Path(migrations_dir or Path(__file__).with_name("migrations"))
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._run_sources: dict[int, str] = {}
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

    def snapshot_owner_scope(self, source: str) -> tuple[bool, str | None]:
        """Which scope last replaced this source's materialized snapshot.

        Returns ``(found, scope)``. The flag matters: "no complete snapshot has
        ever run" and "the snapshot belongs to the unscoped legacy history" are
        different situations, and a bare None cannot tell them apart.

        Current rows are still isolated by source alone, so a complete rescan
        under one scope retires every other scope's rows. A cursor earned before
        that retirement is still a truthful statement about the provider — and
        completely wrong about what is on disk. Callers compare this against
        their own scope and decline the cursor when it does not match.
        """
        row = self.conn.execute(
            "SELECT cursor_scope FROM runs WHERE source = ? AND complete_snapshot = 1 "
            "AND state = 'succeeded' ORDER BY id DESC LIMIT 1",
            (source,),
        ).fetchone()
        if row is None:
            return False, None
        scope = row["cursor_scope"]
        return True, (str(scope) if scope is not None else None)

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

    def latest_successful_run(self) -> tuple[int, str]:
        """Return the newest successful run, or ``(0, "unknown")`` when there is none."""
        row = self.conn.execute(
            "SELECT id, source FROM runs WHERE state = 'succeeded' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return (int(row["id"]), str(row["source"])) if row else (0, "unknown")

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

    def begin_immediate(self) -> None:
        """Take the write lock now, so a concurrent ingest cannot slip in later.

        Reading the latest run and appending decisions must be one atomic step;
        otherwise an ingest committing between them makes the runtime record
        exactly the stale judgment the contract says must fail closed.
        """
        self.conn.execute("BEGIN IMMEDIATE")

    def _retire_version(
        self, run_id: int, source: str, version_id: int, transaction_id: str, reason: str
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
            "INSERT INTO retirement_record (transaction_id, source, prior_version_id, run_id,"
            " reason, retired_at) VALUES (?, ?, ?, ?, ?, ?)",
            (transaction_id, source, version_id, run_id, reason, _now()),
        )

    def retire_absent_snapshot(self, run_id: int, observed_ids: set[str]) -> int:
        """Retire current rows absent from a complete replacement snapshot.

        Recorded as an inference (`absent_from_snapshot`), never as a deletion.
        The provider said nothing about these transactions; we concluded they
        were gone because a scan we believed complete did not mention them. If
        that belief was wrong the records here are what makes it recoverable.
        """
        source = self._run_sources.get(run_id)
        if source is None:
            row = self.conn.execute("SELECT source FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise sqlite3.DatabaseError(f"unknown run ID: {run_id}")
            source = str(row["source"])
        current = self.conn.execute(
            "SELECT id, transaction_id FROM transaction_version "
            "WHERE source = ? AND is_current = 1",
            (source,),
        ).fetchall()
        retired = 0
        for row in current:
            if row["transaction_id"] not in observed_ids:
                self._retire_version(
                    run_id, source, int(row["id"]), str(row["transaction_id"]), RETIRED_BY_ABSENCE
                )
                retired += 1
        return retired

    def retirements(
        self, source: str | None = None, transaction_id: str | None = None
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
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT id, transaction_id, source, prior_version_id, run_id, reason, retired_at "
                f"FROM retirement_record{where} ORDER BY id",
                params,
            )
        ]

    def retired_transaction_ids(self, source: str) -> set[str]:
        """Transactions currently retired: ever retired, and not since restored.

        A retirement is not permanent — a provider can resurrect a transaction,
        and a later run makes it current again. Reading the retirement table
        alone would treat those as still gone, so membership is confirmed
        against the absence of a current version.
        """
        return {
            str(row["transaction_id"])
            for row in self.conn.execute(
                "SELECT DISTINCT r.transaction_id FROM retirement_record r "
                "WHERE r.source = ? AND NOT EXISTS ("
                "  SELECT 1 FROM transaction_version v WHERE v.transaction_id = r.transaction_id"
                "  AND v.source = r.source AND v.is_current = 1)",
                (source,),
            )
        }

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
        """Append a version if the content hash changed. Returns 'new'|'changed'|'same'."""
        source = self._run_sources.get(run_id)
        if source is None:
            row = self.conn.execute("SELECT source FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise sqlite3.DatabaseError(f"unknown run ID: {run_id}")
            source = str(row["source"])

        txid = record["transaction_id"]
        if record.get("is_deleted"):
            # Select before updating: the retirement record names the exact
            # version it retired, and a blind UPDATE discards the identity that
            # makes a repeated retire/reappear/retire cycle readable afterwards.
            current = self.conn.execute(
                "SELECT id FROM transaction_version "
                "WHERE transaction_id = ? AND source = ? AND is_current = 1",
                (txid, source),
            ).fetchall()
            for row in current:
                self._retire_version(
                    run_id, source, int(row["id"]), str(txid), RETIRED_BY_TOMBSTONE
                )
            return "deleted" if current else "deleted_missing"

        shash = self.source_hash(record)
        row = self.conn.execute(
            "SELECT id, source_hash, algorithm_version, ruleset_version "
            "FROM transaction_version WHERE transaction_id = ? AND source = ? "
            "AND is_current = 1",
            (txid, source),
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
            "algorithm_version",
            "ruleset_version",
            "posted_on",
            "transacted_on",
            "account_name",
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
            "algorithm_version": ALGORITHM_VERSION,
            "ruleset_version": RULESET_VERSION,
        }
        for column in ("excluded_from_f2s", "is_split", "is_reviewed"):
            values.setdefault(column, 0)
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
