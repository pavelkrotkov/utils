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
RULESET_VERSION = "0.1.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path, migrations_dir: Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrations_dir = Path(migrations_dir or Path(__file__).with_name("migrations"))
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
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
            self.conn.executescript(sql_file.read_text(encoding="utf-8"))
            self.conn.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (sql_file.name, _now()),
            )
            self.conn.commit()

    # --- runs ---------------------------------------------------------------

    def start_run(self, source: str, source_detail: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, source, source_detail,"
            " algorithm_version, ruleset_version) VALUES (?, ?, ?, ?, ?)",
            (_now(), source, source_detail, ALGORITHM_VERSION, RULESET_VERSION),
        )
        self.conn.commit()
        if cur.lastrowid is None:
            raise sqlite3.DatabaseError("SQLite did not return a run ID")
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, outcome: str, row_count: int) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, outcome = ?, row_count = ? WHERE id = ?",
            (_now(), outcome, row_count, run_id),
        )
        self.conn.commit()

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
        txid = record["transaction_id"]
        shash = self.source_hash(record)
        row = self.conn.execute(
            "SELECT id, source_hash FROM transaction_version"
            " WHERE transaction_id = ? AND is_current = 1",
            (txid,),
        ).fetchone()

        if row and row["source_hash"] == shash:
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
        ]
        values = {
            **record,
            "run_id": run_id,
            "observed_at": _now(),
            "source_hash": shash,
            "is_current": 1,
        }
        for column in ("excluded_from_f2s", "is_split", "is_reviewed"):
            values.setdefault(column, 0)
        self.conn.execute(
            f"INSERT INTO transaction_version ({','.join(cols)})"
            f" VALUES ({','.join('?' * len(cols))})",
            [values.get(c) for c in cols],
        )
        return "changed" if row else "new"

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
