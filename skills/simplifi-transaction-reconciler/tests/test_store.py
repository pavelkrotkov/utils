from pathlib import Path

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
