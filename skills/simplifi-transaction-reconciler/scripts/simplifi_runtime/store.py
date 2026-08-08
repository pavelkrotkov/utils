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
        outcome: str,
        row_count: int,
        cursor_after: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, outcome = ?, row_count = ?, cursor_after = ? "
            "WHERE id = ?",
            (_now(), outcome, row_count, cursor_after, run_id),
        )

    def latest_cursor(self, source: str) -> str | None:
        row = self.conn.execute(
            "SELECT cursor_after FROM runs WHERE source = ? AND outcome = 'success' "
            "AND cursor_after IS NOT NULL ORDER BY id DESC LIMIT 1",
            (source,),
        ).fetchone()
        return str(row["cursor_after"]) if row else None

    def latest_successful_run(self) -> tuple[int, str]:
        """Return the newest successful run, or ``(0, "unknown")`` when there is none."""
        row = self.conn.execute(
            "SELECT id, source FROM runs WHERE outcome = 'success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return (int(row["id"]), str(row["source"])) if row else (0, "unknown")

    def begin_immediate(self) -> None:
        """Take the write lock now, so a concurrent ingest cannot slip in later.

        Reading the latest run and appending decisions must be one atomic step;
        otherwise an ingest committing between them makes the runtime record
        exactly the stale judgment the contract says must fail closed.
        """
        self.conn.execute("BEGIN IMMEDIATE")

    def retire_absent_snapshot(self, run_id: int, observed_ids: set[str]) -> int:
        """Retire current rows absent from a complete replacement snapshot."""
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
                self.conn.execute(
                    "UPDATE transaction_version SET is_current = 0 WHERE id = ?",
                    (row["id"],),
                )
                retired += 1
        return retired

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
            result = self.conn.execute(
                "UPDATE transaction_version SET is_current = 0 "
                "WHERE transaction_id = ? AND source = ? AND is_current = 1",
                (txid, source),
            )
            return "deleted" if result.rowcount else "deleted_missing"

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
