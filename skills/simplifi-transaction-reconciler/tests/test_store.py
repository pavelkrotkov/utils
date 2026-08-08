import sqlite3
from pathlib import Path

import pytest
from simplifi_runtime.store import Store


def _record(transaction_id: str) -> dict:
    return {
        "transaction_id": transaction_id,
        "posted_on": "2026-08-01",
        "transacted_on": None,
        "account_name": "Checking",
        "account_id": "checking-1",
        "amount_minor_units": -5000,
        "currency": "USD",
        "currency_exponent": 2,
        "payee_raw": "Example Store",
        "payee_normalized": "Example Store",
        "payee_canonical": "example_store",
        "payee_display": "Example Store",
        "norm_rules_applied": "",
        "original_currency": None,
        "original_amount": None,
        "is_foreign_charge": 0,
        "category": "Shopping",
        "inferred_category": None,
        "is_uncategorized": 0,
        "exclusion_flag": 0,
        "excluded_from_f2s": 0,
        "recurring_flag": 0,
        "is_split": 0,
        "is_reviewed": 0,
        "kind": "spend",
        "poisons_statistics": 0,
        "semantics_reasons": "",
        "txn_state": "CLEARED",
        "match_state": None,
        "scheduled_model_id": None,
        "scheduled_due_on": None,
    }


def test_sources_are_isolated_and_tombstones_retire_only_their_source(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    csv_run = store.start_run("csv", "fixture")
    api_run = store.start_run("api", "fixture")

    assert store.upsert_version(csv_run, _record("txn-1")) == "new"
    assert store.upsert_version(api_run, _record("txn-1")) == "new"
    assert (
        store.conn.execute(
            "SELECT COUNT(*) FROM transaction_version WHERE is_current = 1"
        ).fetchone()[0]
        == 2
    )

    assert (
        store.upsert_version(api_run, {"transaction_id": "txn-1", "is_deleted": True}) == "deleted"
    )
    current = store.conn.execute(
        "SELECT source FROM transaction_version WHERE is_current = 1"
    ).fetchall()
    assert [row[0] for row in current] == ["csv"]
    store.close()


def test_eligibility_diagnostics_are_persisted(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("csv", "fixture")
    record = _record("txn-1")
    record.update(
        review_eligible=1,
        eligibility_reason_codes="missing_optional_field,eligible",
    )

    assert store.upsert_version(run_id, record) == "new"
    saved = store.conn.execute(
        "SELECT review_eligible, eligibility_reason_codes FROM transaction_version"
    ).fetchone()

    assert tuple(saved) == (1, "missing_optional_field,eligible")
    store.close()


def test_derivation_version_change_appends_a_version(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("csv", "fixture")
    record = _record("txn-1")
    assert store.upsert_version(run_id, record) == "new"

    store.conn.execute("UPDATE transaction_version SET ruleset_version = '0.0.0'")
    assert store.upsert_version(run_id, record) == "changed"
    assert (
        store.conn.execute(
            "SELECT COUNT(*) FROM transaction_version WHERE transaction_id = 'txn-1'"
        ).fetchone()[0]
        == 2
    )
    store.close()


def test_failed_run_can_be_rolled_back(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("csv", "fixture")
    store.upsert_version(run_id, _record("txn-1"))
    store.rollback()

    assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM transaction_version").fetchone()[0] == 0
    store.close()


def test_failed_run_is_audited_after_transaction_work_is_rolled_back(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("csv", "fixture")
    store.commit()
    store.upsert_version(run_id, _record("txn-1"))

    store.rollback()
    store.finish_run(run_id, "failure", 0)
    store.commit()

    run = store.conn.execute("SELECT outcome FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run[0] == "failure"
    assert store.conn.execute("SELECT COUNT(*) FROM transaction_version").fetchone()[0] == 0
    store.close()


def test_latest_successful_api_cursor_is_persisted(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("api", "fixture", cursor_before="2026-08-01T00:00:00Z")
    store.finish_run(run_id, "success", 2, cursor_after="2026-08-06T12:00:00Z")
    store.commit()

    assert store.latest_cursor("api") == "2026-08-06T12:00:00Z"
    store.close()


def test_failed_run_never_advances_the_cursor(tmp_path: Path):
    """A later failure must not overwrite the last cursor that was earned."""
    store = Store(tmp_path / "review.sqlite")
    good = store.start_run("api", "fixture", cursor_before=None)
    store.finish_run(good, "success", 2, cursor_after="2026-08-06T12:00:00Z")
    bad = store.start_run("api", "fixture", cursor_before="2026-08-06T12:00:00Z")
    store.finish_run(bad, "failure", 0)
    store.commit()

    assert store.latest_cursor("api") == "2026-08-06T12:00:00Z"
    store.close()


def test_csv_replacement_snapshot_retires_absent_and_edited_rows(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    first_run = store.start_run("csv", "first snapshot")
    store.upsert_version(first_run, _record("txn-1"))
    store.upsert_version(first_run, _record("txn-2"))
    store.retire_absent_snapshot(first_run, {"txn-1", "txn-2"})
    store.commit()

    second_run = store.start_run("csv", "replacement snapshot")
    store.upsert_version(second_run, _record("txn-1-edited"))
    retired = store.retire_absent_snapshot(second_run, {"txn-1-edited"})
    store.commit()

    assert retired == 2
    current = store.conn.execute(
        "SELECT transaction_id FROM transaction_version WHERE source = 'csv' AND is_current = 1"
    ).fetchall()
    assert [row[0] for row in current] == ["txn-1-edited"]
    store.close()


def _decision(run_id: int, decision_id: str, **overrides) -> dict:
    record = {
        "decision_id": decision_id,
        "run_id": run_id,
        "source": "csv",
        "analysis_date": "2026-08-15",
        "transaction_id": "txn-1",
        "proposal_id": "proposal-1",
        "proposal_hash": "a" * 64,
        "dataset_hash": "b" * 64,
        "decision": "accept",
        "action": "record_category_proposal",
        "category": "Shopping",
        "rationale": "Cleared charge matches the merchant's established history.",
        "reviewer_kind": "agent",
        "reviewer_id": "review-agent",
        "recorded_at": "2026-08-16T09:00:00+00:00",
        "validator_version": "1.0.0",
    }
    record.update(overrides)
    return record


def test_decision_records_are_append_only(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("csv", "fixture")
    store.append_decision_records([_decision(run_id, "decision-1")])
    store.commit()

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("UPDATE decision_record SET decision = 'reject'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM decision_record")

    stored = store.decision_records(run_id)
    assert [record["decision"] for record in stored] == ["accept"]
    store.close()


def test_recording_the_same_decision_twice_appends_once(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("csv", "fixture")
    record = _decision(run_id, "decision-1")

    assert store.append_decision_records([record]) == 1
    assert store.append_decision_records([record]) == 0

    revised = _decision(run_id, "decision-2", decision="reject", category=None)
    assert store.append_decision_records([revised]) == 1
    store.commit()

    stored = store.decision_records()
    assert [item["decision_id"] for item in stored] == ["decision-1", "decision-2"]
    assert [item["decision"] for item in stored] == ["accept", "reject"]
    store.close()


def test_stored_decisions_return_the_database_copy_not_the_candidate(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("csv", "fixture")
    store.append_decision_records([_decision(run_id, "decision-1")])
    store.commit()

    candidate = _decision(run_id, "decision-1", recorded_at="2026-09-01T12:00:00+00:00")
    assert store.append_decision_records([candidate]) == 0
    stored = store.stored_decisions(["decision-1", "decision-missing"])

    assert [record["decision_id"] for record in stored] == ["decision-1"]
    assert stored[0]["recorded_at"] == "2026-08-16T09:00:00+00:00"
    assert "id" not in stored[0], "the internal rowid must not reach exported records"
    store.close()


def test_latest_successful_run_ignores_unfinished_and_failed_runs(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    assert store.latest_successful_run() == (0, "unknown")

    good = store.start_run("csv", "good")
    store.finish_run(good, "success", 1)
    failed = store.start_run("api", "bad")
    store.finish_run(failed, "failure", 0)
    store.start_run("api", "still running")
    store.commit()

    assert store.latest_successful_run() == (good, "csv")
    store.close()


def test_begin_immediate_takes_the_write_lock(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    store.begin_immediate()
    store.start_run("csv", "holding the lock")

    other = sqlite3.connect(tmp_path / "review.sqlite", timeout=0.1)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        other.execute(
            "INSERT INTO runs (started_at, source, algorithm_version, ruleset_version)"
            " VALUES ('now', 'csv', '0.1.0', '0.2.0')"
        )
    other.close()

    store.rollback()
    store.close()


def test_decision_records_require_a_known_run(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    store.start_run("csv", "fixture")

    with pytest.raises(sqlite3.IntegrityError):
        store.append_decision_records([_decision(999, "decision-1")])
    store.close()


def test_schema_migration_rolls_back_all_statements(tmp_path: Path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_broken.sql").write_text(
        "CREATE TABLE migration_probe (id INTEGER);\nSELECT missing_column FROM nowhere;",
        encoding="utf-8",
    )

    with pytest.raises(sqlite3.Error):
        Store(tmp_path / "review.sqlite", migrations_dir=migrations)

    with sqlite3.connect(tmp_path / "review.sqlite") as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'migration_probe'"
            ).fetchone()
            is None
        )
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
