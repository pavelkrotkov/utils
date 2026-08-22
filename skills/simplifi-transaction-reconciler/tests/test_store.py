import sqlite3
from pathlib import Path

import pytest
from simplifi_runtime.store import (
    RETIRED_BY_ABSENCE,
    RETIRED_BY_TOMBSTONE,
    RUN_ABORTED,
    RUN_FAILED,
    RUN_STARTED,
    RUN_SUCCEEDED,
    Store,
)
from simplifi_runtime.sync_scope import csv_scope


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
    store.finish_run(run_id, RUN_FAILED, 0)
    store.commit()

    run = store.conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run[0] == RUN_FAILED
    assert store.conn.execute("SELECT COUNT(*) FROM transaction_version").fetchone()[0] == 0
    store.close()


def test_latest_successful_api_cursor_is_persisted(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("api", "fixture", cursor_before="2026-08-01T00:00:00Z")
    store.finish_run(run_id, RUN_SUCCEEDED, 2, cursor_after="2026-08-06T12:00:00Z")
    store.commit()

    assert store.latest_cursor("api") == "2026-08-06T12:00:00Z"
    store.close()


def test_cursors_are_isolated_by_scope(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    first = store.start_run("api", "dataset one")
    store.record_run_scope(first, None, '{"dataset":"one"}')
    store.finish_run(first, RUN_SUCCEEDED, 1, cursor_after="2026-08-06T12:00:00Z")
    second = store.start_run("api", "dataset two")
    store.record_run_scope(second, None, '{"dataset":"two"}')
    store.finish_run(second, RUN_SUCCEEDED, 1, cursor_after="2026-01-01T00:00:00Z")
    store.commit()

    assert store.latest_cursor("api", '{"dataset":"one"}') == "2026-08-06T12:00:00Z"
    assert store.latest_cursor("api", '{"dataset":"two"}') == "2026-01-01T00:00:00Z"
    assert store.latest_cursor("api", '{"dataset":"three"}') is None
    store.close()


def test_unscoped_lookup_does_not_see_scoped_cursors(tmp_path: Path):
    """`IS NULL` is a scope, not a wildcard — otherwise every legacy caller
    would silently adopt whichever scope happened to run last."""
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("api", "scoped")
    store.record_run_scope(run_id, None, '{"dataset":"one"}')
    store.finish_run(run_id, RUN_SUCCEEDED, 1, cursor_after="2026-08-06T12:00:00Z")
    store.commit()

    assert store.latest_cursor("api") is None
    assert store.has_unscoped_cursor("api") is False
    store.close()


def test_legacy_unscoped_cursor_is_visible_but_not_borrowed(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("api", "pre-scoping run")
    store.finish_run(run_id, RUN_SUCCEEDED, 1, cursor_after="2026-07-01T00:00:00Z")
    store.commit()

    assert store.has_unscoped_cursor("api") is True
    assert store.latest_cursor("api", '{"dataset":"one"}') is None
    assert store.latest_cursor("api") == "2026-07-01T00:00:00Z"
    store.close()


def test_record_run_scope_persists_scope_cursor_and_detail(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("api", "unresolved")
    store.record_run_scope(run_id, "2026-08-01T00:00:00Z", '{"dataset":"one"}', "api scope=…")
    store.commit()

    row = store.conn.execute(
        "SELECT cursor_before, cursor_scope, source_detail FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["cursor_before"] == "2026-08-01T00:00:00Z"
    assert row["cursor_scope"] == '{"dataset":"one"}'
    assert row["source_detail"] == "api scope=…"
    store.close()


def test_a_rescan_retires_only_its_own_scope(tmp_path: Path):
    """The collision migration 011 could only survive, migration 015 prevents.

    Before state was scoped, a complete rescan under one scope retired every
    other scope's rows: they were absent from the observed-ID set and nothing
    on the row said they belonged to a different dataset.
    """
    store = Store(tmp_path / "review.sqlite")
    first = store.start_run("api", "scope one")
    store.record_run_scope(first, None, '{"dataset":"one"}')
    store.upsert_version(first, _record("txn-one"))
    store.finish_run(first, RUN_SUCCEEDED, 1, complete_snapshot=True)

    second = store.start_run("api", "scope two, complete rescan")
    store.record_run_scope(second, None, '{"dataset":"two"}')
    store.upsert_version(second, _record("txn-two"))
    retired = store.retire_absent_snapshot(second, {"txn-two"})
    store.finish_run(second, RUN_SUCCEEDED, 1, complete_snapshot=True)
    store.commit()

    assert retired == 0, "scope one's rows are not absent, they are not asked about"
    assert store.retired_transaction_ids("api", '{"dataset":"one"}') == set()
    assert store.current_scopes("api") == ['{"dataset":"one"}', '{"dataset":"two"}']
    store.close()


def test_a_rescan_still_retires_absences_inside_its_own_scope(tmp_path: Path):
    """Scoping must not turn the absence inference off, only aim it."""
    store = Store(tmp_path / "review.sqlite")
    first = store.start_run("api", "seed")
    store.record_run_scope(first, None, '{"dataset":"one"}')
    store.upsert_version(first, _record("txn-gone"))
    store.finish_run(first, RUN_SUCCEEDED, 1, complete_snapshot=True)

    second = store.start_run("api", "rescan without it")
    store.record_run_scope(second, None, '{"dataset":"one"}')
    retired = store.retire_absent_snapshot(second, set())
    store.commit()

    assert retired == 1
    assert store.retired_transaction_ids("api", '{"dataset":"one"}') == {"txn-gone"}
    store.close()


def test_the_same_transaction_id_in_two_scopes_stays_two_rows(tmp_path: Path):
    """Provider IDs are unique within a dataset, not across datasets."""
    store = Store(tmp_path / "review.sqlite")
    one = store.start_run("api", "scope one")
    store.record_run_scope(one, None, '{"dataset":"one"}')
    store.upsert_version(one, _record("txn-1"))
    two = store.start_run("api", "scope two")
    store.record_run_scope(two, None, '{"dataset":"two"}')
    assert store.upsert_version(two, _record("txn-1")) == "new", (
        "the second scope has never seen this transaction"
    )
    store.commit()

    current = store.conn.execute(
        "SELECT cursor_scope FROM transaction_version WHERE is_current = 1 ORDER BY cursor_scope"
    ).fetchall()
    assert [row["cursor_scope"] for row in current] == ['{"dataset":"one"}', '{"dataset":"two"}']
    store.close()


def test_legacy_rows_are_adopted_only_where_a_rescan_returned_them(tmp_path: Path):
    """Adoption is by evidence: the provider returned this ID for this scope."""
    store = Store(tmp_path / "review.sqlite")
    legacy = store.start_run("api", "before scoping")
    store.upsert_version(legacy, _record("txn-mine"))
    store.upsert_version(legacy, _record("txn-theirs"))
    store.finish_run(legacy, RUN_SUCCEEDED, 2)
    store.commit()
    assert store.unattributed_row_count("api") == 2

    scoped = store.start_run("api", "complete rescan of dataset one")
    store.record_run_scope(scoped, None, '{"dataset":"one"}')
    assert store.adopt_legacy_scope(scoped, {"txn-mine"}) == 1
    store.commit()

    # The row this dataset never returned stays unattributed, for another
    # scope's rescan to claim.
    assert store.unattributed_row_count("api") == 1
    assert store.current_scopes("api") == [None, '{"dataset":"one"}']
    store.close()


def test_an_incremental_run_never_adopts(tmp_path: Path):
    """Only a complete snapshot's observed set means "everything this dataset holds"."""
    store = Store(tmp_path / "review.sqlite")
    legacy = store.start_run("api", "before scoping")
    store.upsert_version(legacy, _record("txn-1"))
    store.finish_run(legacy, RUN_SUCCEEDED, 1)
    store.commit()

    # The CLI only calls adopt on a complete snapshot; with no observed IDs
    # there is nothing to claim either way.
    scoped = store.start_run("api", "incremental")
    store.record_run_scope(scoped, None, '{"dataset":"one"}')
    assert store.adopt_legacy_scope(scoped, set()) == 0
    store.commit()

    assert store.unattributed_row_count("api") == 1
    store.close()


def test_orphaned_legacy_rows_can_be_retired_explicitly(tmp_path: Path):
    """The escape hatch, once every scope has been rebuilt."""
    store = Store(tmp_path / "review.sqlite")
    legacy = store.start_run("api", "before scoping")
    store.upsert_version(legacy, _record("txn-gone"))
    store.finish_run(legacy, RUN_SUCCEEDED, 1)
    store.commit()

    scoped = store.start_run("api", "rescan that never returned it")
    store.record_run_scope(scoped, None, '{"dataset":"one"}')
    assert store.retire_orphaned_legacy(scoped) == 1
    store.commit()

    assert store.unattributed_row_count("api") == 0
    # Recorded as the inference it is, not as a provider deletion.
    assert [item["reason"] for item in store.retirements(source="api")] == [RETIRED_BY_ABSENCE]
    store.close()


def test_supersession_is_asked_per_scope(tmp_path: Path):
    """An unrelated dataset's ingest must not invalidate another's packet."""
    store = Store(tmp_path / "review.sqlite")
    one = store.start_run("api", "scope one")
    store.record_run_scope(one, None, '{"dataset":"one"}')
    store.finish_run(one, RUN_SUCCEEDED, 1)
    two = store.start_run("api", "scope two, later")
    store.record_run_scope(two, None, '{"dataset":"two"}')
    store.finish_run(two, RUN_SUCCEEDED, 1)
    store.commit()

    assert store.latest_successful_run() == (two, "api", '{"dataset":"two"}')
    # Scope one is still its own latest, despite two having run afterwards.
    assert store.latest_successful_run_in_scope("api", '{"dataset":"one"}') == (
        one,
        "api",
        '{"dataset":"one"}',
    )
    store.close()


def test_a_csv_run_never_writes_into_the_legacy_scope(tmp_path: Path):
    """CSV has one scope, but it is a named one — the legacy bucket is drained."""
    assert csv_scope().key() is not None
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("csv", "export")
    store.record_run_scope(run_id, None, csv_scope().key())
    store.upsert_version(run_id, _record("txn-1"))
    store.commit()

    assert store.current_scopes("csv") == [csv_scope().key()]
    assert store.unattributed_row_count("csv") == 0
    store.close()


def test_failed_run_never_advances_the_cursor(tmp_path: Path):
    """A later failure must not overwrite the last cursor that was earned."""
    store = Store(tmp_path / "review.sqlite")
    good = store.start_run("api", "fixture", cursor_before=None)
    store.finish_run(good, RUN_SUCCEEDED, 2, cursor_after="2026-08-06T12:00:00Z")
    bad = store.start_run("api", "fixture", cursor_before="2026-08-06T12:00:00Z")
    store.finish_run(bad, RUN_FAILED, 0)
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
    assert store.latest_successful_run() == (0, "unknown", None)

    good = store.start_run("csv", "good")
    store.finish_run(good, RUN_SUCCEEDED, 1)
    failed = store.start_run("api", "bad")
    store.finish_run(failed, RUN_FAILED, 0)
    store.start_run("api", "still running")
    store.commit()

    assert store.latest_successful_run() == (good, "csv", None)
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


def test_finish_run_rejects_a_state_outside_the_lifecycle(tmp_path: Path):
    """A typo must fail loudly, not write a value no query matches.

    Storing "success" would leave the run looking finished in the table while
    being invisible to every reader — precisely the confusion the explicit
    lifecycle exists to remove.
    """
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("csv", "fixture")

    for bad in ("success", "failure", RUN_STARTED, "", "SUCCEEDED"):
        with pytest.raises(ValueError, match="run state must be one of"):
            store.finish_run(run_id, bad, 0)
    store.close()


def test_a_new_run_starts_in_the_started_state(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("csv", "fixture")
    store.commit()

    row = store.conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["state"] == RUN_STARTED
    store.close()


def test_failed_run_records_its_cause(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("api", "fixture")
    store.finish_run(
        run_id, RUN_FAILED, 0, error_class="AuthError", error_message="token expired; refresh it"
    )
    store.commit()

    row = store.conn.execute(
        "SELECT state, error_class, error_message FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert (row["state"], row["error_class"]) == (RUN_FAILED, "AuthError")
    assert "refresh it" in row["error_message"]
    store.close()


def test_legacy_outcome_mirrors_state_without_drifting(tmp_path: Path):
    """`outcome` is written from `state` alone, so the two cannot disagree."""
    store = Store(tmp_path / "review.sqlite")
    for state, expected in (
        (RUN_SUCCEEDED, "success"),
        (RUN_FAILED, "failure"),
        (RUN_ABORTED, "failure"),
    ):
        run_id = store.start_run("api", "fixture")
        store.finish_run(run_id, state, 0)
        row = store.conn.execute(
            "SELECT state, outcome FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert (row["state"], row["outcome"]) == (state, expected)
    store.commit()
    store.close()


def test_aborted_and_failed_runs_are_both_excluded_from_analysis_input(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    for state in (RUN_FAILED, RUN_ABORTED):
        run_id = store.start_run("api", "fixture")
        store.finish_run(run_id, state, 0, cursor_after="2026-08-06T12:00:00Z")
    store.start_run("api", "still running")
    store.commit()

    assert store.latest_successful_run() == (0, "unknown", None)
    # Nor may either contribute a cursor, whatever they managed to record.
    assert store.latest_cursor("api") is None
    store.close()


def test_latest_run_summary_reports_the_newest_run_of_any_state(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    good = store.start_run("csv", "fixture")
    store.finish_run(good, RUN_SUCCEEDED, 1)
    bad = store.start_run("api", "fixture")
    store.finish_run(bad, RUN_FAILED, 0, error_class="ApiError", error_message="500")
    store.commit()

    summary = store.latest_run_summary()

    assert summary is not None
    assert (summary["id"], summary["state"], summary["source"]) == (bad, RUN_FAILED, "api")
    assert summary["error_class"] == "ApiError"
    store.close()


def test_latest_run_summary_is_none_for_an_empty_database(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")

    assert store.latest_run_summary() is None
    store.close()


def test_migration_backfills_state_from_legacy_outcome(tmp_path: Path):
    """An upgraded database must classify its history, not lose it.

    NULL becomes 'aborted' rather than 'started': every pre-migration run
    belongs to a process that is long gone, and calling it "in progress" would
    keep a dead run eligible forever.
    """
    db = tmp_path / "legacy.sqlite"
    store = Store(db)
    store.close()

    # Rewind to the pre-lifecycle schema and write rows the old code would have.
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE name = '012_run_lifecycle.sql'")
        conn.execute("DROP INDEX IF EXISTS idx_runs_state")
        for table_column in ("state", "error_class", "error_message"):
            conn.execute(f"ALTER TABLE runs DROP COLUMN {table_column}")
        for outcome in ("success", "failure", None):
            conn.execute(
                "INSERT INTO runs (started_at, source, source_detail, algorithm_version,"
                " ruleset_version, outcome) VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-01-01T00:00:00+00:00", "api", "legacy", "0.1.0", "0.2.0", outcome),
            )

    reopened = Store(db)
    try:
        states = [
            row["state"] for row in reopened.conn.execute("SELECT state FROM runs ORDER BY id")
        ]
        assert states == [RUN_SUCCEEDED, RUN_FAILED, RUN_ABORTED]
        # The successful legacy run stays usable; the other two never become input.
        assert reopened.latest_successful_run()[1] == "api"
    finally:
        reopened.close()


def test_a_terminal_run_cannot_be_transitioned_again(tmp_path: Path):
    """Terminal means terminal, whatever calls in afterwards.

    An error raised after the ingest committed reaches the failure path with a
    rollback that can no longer take back the committed rows. Rewriting the run
    as failed would leave current transaction rows beside a run claiming it
    failed, so the transition is refused and the caller told nothing changed.
    """
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("api", "fixture")

    assert store.finish_run(run_id, RUN_SUCCEEDED, 9, cursor_after="2026-08-06T12:00:00Z") is True
    assert store.finish_run(run_id, RUN_FAILED, 0, error_class="BrokenPipeError") is False

    row = store.conn.execute(
        "SELECT state, row_count, cursor_after, error_class FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["state"] == RUN_SUCCEEDED
    assert row["row_count"] == 9
    assert row["cursor_after"] == "2026-08-06T12:00:00Z"
    assert row["error_class"] is None
    store.close()


def test_a_started_run_transitions_exactly_once(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("api", "fixture")

    assert store.finish_run(run_id, RUN_FAILED, 0, error_class="ApiError") is True
    assert store.finish_run(run_id, RUN_ABORTED, 0) is False

    assert (
        store.conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()["state"]
        == RUN_FAILED
    )
    store.close()


def test_tombstone_and_absence_are_recorded_as_different_reasons(tmp_path: Path):
    """The distinction is the point: testimony versus inference.

    A tombstone is the provider stating the transaction was deleted. An absence
    is our own conclusion from a scan we believed complete — and a truncated
    response mistaken for a complete one produces a wave of the second kind
    that looks exactly like the first.
    """
    store = Store(tmp_path / "review.sqlite")
    first = store.start_run("api", "seed")
    store.upsert_version(first, _record("txn-tombstoned"))
    store.upsert_version(first, _record("txn-absent"))
    store.upsert_version(first, _record("txn-kept"))

    second = store.start_run("api", "tombstone")
    assert store.upsert_version(second, {"transaction_id": "txn-tombstoned", "is_deleted": True})

    third = store.start_run("api", "complete scan")
    assert store.retire_absent_snapshot(third, {"txn-kept"}) == 1
    store.commit()

    by_id = {item["transaction_id"]: item for item in store.retirements(source="api")}
    assert by_id["txn-tombstoned"]["reason"] == RETIRED_BY_TOMBSTONE
    assert by_id["txn-tombstoned"]["run_id"] == second
    assert by_id["txn-absent"]["reason"] == RETIRED_BY_ABSENCE
    assert by_id["txn-absent"]["run_id"] == third
    assert "txn-kept" not in by_id
    store.close()


def test_a_retirement_record_carries_everything_needed_to_reconstruct_it(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    seed = store.start_run("api", "seed")
    store.upsert_version(seed, _record("txn-1"))
    version_id = store.conn.execute(
        "SELECT id FROM transaction_version WHERE transaction_id = 'txn-1'"
    ).fetchone()["id"]

    retiring = store.start_run("api", "tombstone")
    store.upsert_version(retiring, {"transaction_id": "txn-1", "is_deleted": True})
    store.commit()

    record = store.retirements(transaction_id="txn-1")[0]
    assert record["transaction_id"] == "txn-1"
    assert record["source"] == "api"
    assert record["prior_version_id"] == version_id
    assert record["run_id"] == retiring
    assert record["reason"] == RETIRED_BY_TOMBSTONE
    assert record["retired_at"]
    store.close()


def test_history_survives_and_current_state_excludes_retired_rows(tmp_path: Path):
    """Retirement hides a transaction from the current view, not from history."""
    store = Store(tmp_path / "review.sqlite")
    seed = store.start_run("api", "seed")
    store.upsert_version(seed, _record("txn-1"))
    retiring = store.start_run("api", "tombstone")
    store.upsert_version(retiring, {"transaction_id": "txn-1", "is_deleted": True})
    store.commit()

    current = store.conn.execute(
        "SELECT COUNT(*) FROM transaction_version WHERE source = 'api' AND is_current = 1"
    ).fetchone()[0]
    assert current == 0
    # The version row itself is untouched, and its retirement is on the record.
    assert (
        store.conn.execute(
            "SELECT COUNT(*) FROM transaction_version WHERE transaction_id = 'txn-1'"
        ).fetchone()[0]
        == 1
    )
    assert len(store.retirements(transaction_id="txn-1")) == 1
    store.close()


def test_repeated_retirements_stay_distinguishable(tmp_path: Path):
    """A transaction can be retired, reappear, and be retired again.

    Each event names the version it retired, so the cycle reads as three
    separate facts rather than one ambiguous flag flipped twice.
    """
    store = Store(tmp_path / "review.sqlite")
    first = store.start_run("api", "seed")
    store.upsert_version(first, _record("txn-1"))
    second = store.start_run("api", "tombstone")
    store.upsert_version(second, {"transaction_id": "txn-1", "is_deleted": True})
    third = store.start_run("api", "reappears")
    store.upsert_version(third, _record("txn-1"))
    fourth = store.start_run("api", "complete scan without it")
    store.retire_absent_snapshot(fourth, set())
    store.commit()

    history = store.retirements(transaction_id="txn-1")
    assert [item["reason"] for item in history] == [RETIRED_BY_TOMBSTONE, RETIRED_BY_ABSENCE]
    assert [item["run_id"] for item in history] == [second, fourth]
    # Different versions, so the two events cannot be confused for one another.
    assert history[0]["prior_version_id"] != history[1]["prior_version_id"]
    store.close()


def test_a_restored_transaction_is_no_longer_reported_as_retired(tmp_path: Path):
    """Retirement is not permanent, so the retired set is not just history."""
    store = Store(tmp_path / "review.sqlite")
    first = store.start_run("api", "seed")
    store.upsert_version(first, _record("txn-1"))
    second = store.start_run("api", "tombstone")
    store.upsert_version(second, {"transaction_id": "txn-1", "is_deleted": True})
    store.commit()

    assert store.retired_transaction_ids("api", None) == {"txn-1"}

    third = store.start_run("api", "provider resurrects it")
    store.upsert_version(third, _record("txn-1"))
    store.commit()

    assert store.retired_transaction_ids("api", None) == set()
    # The history of it having been retired is still there.
    assert len(store.retirements(transaction_id="txn-1")) == 1
    store.close()


def test_retirement_is_scoped_to_its_own_source(tmp_path: Path):
    store = Store(tmp_path / "review.sqlite")
    csv_run = store.start_run("csv", "seed")
    api_run = store.start_run("api", "seed")
    store.upsert_version(csv_run, _record("txn-1"))
    store.upsert_version(api_run, _record("txn-1"))
    tombstone = store.start_run("api", "tombstone")
    store.upsert_version(tombstone, {"transaction_id": "txn-1", "is_deleted": True})
    store.commit()

    assert store.retired_transaction_ids("api", None) == {"txn-1"}
    assert store.retired_transaction_ids("csv", None) == set()
    assert [item["source"] for item in store.retirements()] == ["api"]
    store.close()


def test_tombstoning_an_unknown_transaction_records_nothing(tmp_path: Path):
    """There is no prior version to point at, so there is no event to record."""
    store = Store(tmp_path / "review.sqlite")
    run_id = store.start_run("api", "tombstone")

    assert store.upsert_version(run_id, {"transaction_id": "ghost", "is_deleted": True}) == (
        "deleted_missing"
    )
    store.commit()

    assert store.retirements() == []
    store.close()


def test_retirement_history_cannot_be_rewritten_or_deleted(tmp_path: Path):
    """Append-only is enforced by the database, not by convention.

    A provenance table defended only by the code that happens to write it today
    is defended by nothing — and a rewritten record still looks authoritative
    afterwards, which is the whole problem.
    """
    store = Store(tmp_path / "review.sqlite")
    seed = store.start_run("api", "seed")
    store.upsert_version(seed, _record("txn-1"))
    retiring = store.start_run("api", "tombstone")
    store.upsert_version(retiring, {"transaction_id": "txn-1", "is_deleted": True})
    store.commit()

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute(f"UPDATE retirement_record SET reason = '{RETIRED_BY_ABSENCE}'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM retirement_record")
    store.rollback()

    assert [item["reason"] for item in store.retirements()] == [RETIRED_BY_TOMBSTONE]
    store.close()


def test_a_current_row_in_another_source_does_not_mask_a_retirement(tmp_path: Path):
    """The retired set is per source, including its current-version check.

    CSV and API identifiers are not interchangeable, so a live `csv` row with
    the same transaction ID says nothing about whether the `api` one is gone.
    """
    store = Store(tmp_path / "review.sqlite")
    csv_run = store.start_run("csv", "seed")
    api_run = store.start_run("api", "seed")
    store.upsert_version(csv_run, _record("txn-1"))
    store.upsert_version(api_run, _record("txn-1"))
    tombstone = store.start_run("api", "tombstone")
    store.upsert_version(tombstone, {"transaction_id": "txn-1", "is_deleted": True})
    store.commit()

    # csv still holds a current txn-1; that must not hide the api retirement.
    assert store.retired_transaction_ids("api", None) == {"txn-1"}
    assert store.retired_transaction_ids("csv", None) == set()
    store.close()
