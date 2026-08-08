import argparse
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from simplifi_runtime.cli import (
    _aggregator_health,
    _analysis_limitations,
    _as_of_rows,
    _csv_safe_text,
    _ensure_model_key,
    _is_complete_snapshot,
    _known_categories,
    _latest_run,
    _model_taxonomy,
    _next_cursor,
    build_parser,
)
from simplifi_runtime.store import RUN_FAILED, RUN_SUCCEEDED, Store

SKILL_DIR = Path(__file__).resolve().parents[1]
ENTRYPOINT = SKILL_DIR / "scripts" / "simplifi_transaction_reconciler.py"
COMMANDS = {"ingest", "analyze", "classify", "decide", "subs", "probe", "schema", "status"}


def test_every_packaged_subcommand_has_a_handler():
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )

    assert set(subparsers.choices) == COMMANDS
    for command in COMMANDS:
        args = parser.parse_args([command])
        assert callable(args.func), f"subcommand {command!r} has no handler"


def test_bundled_entrypoint_help_runs_from_skill_folder():
    result = subprocess.run(
        [sys.executable, str(ENTRYPOINT), "--help"],
        cwd=SKILL_DIR,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "ingest" in result.stdout
    assert "analyze" in result.stdout


def test_as_of_rows_excludes_future_settled_activity_but_keeps_projections():
    rows = [
        {"posted_on": "2026-08-01", "txn_state": "CLEARED"},
        {
            "posted_on": "2026-09-01",
            "txn_state": "PENDING",
            "scheduled_model_id": "scheduled-1",
        },
        {"posted_on": "2026-09-01", "txn_state": "PENDING"},
    ]

    result = _as_of_rows(rows, date(2026, 8, 15))

    assert result == rows[:2]


def test_classify_rejects_non_positive_chunk_size():
    for value in ("0", "-1"):
        try:
            build_parser().parse_args(["classify", "--chunk-size", value])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"accepted invalid chunk size {value}")


def test_api_ingest_supports_explicit_full_rescan():
    args = build_parser().parse_args(["ingest", "--source", "api", "--full-rescan"])

    assert args.full_rescan
    assert args.modified_after is None

    try:
        build_parser().parse_args(
            ["ingest", "--source", "api", "--full-rescan", "--modified-after", "cursor"]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("accepted mutually exclusive API cursor options")


def test_snapshot_retirement_is_limited_to_complete_imports():
    csv_args = build_parser().parse_args(["ingest", "--source", "csv", "fixture.csv"])
    api_full_args = build_parser().parse_args(["ingest", "--source", "api", "--full-rescan"])
    api_partial_args = build_parser().parse_args(
        ["ingest", "--source", "api", "--full-rescan", "--since", "2026-01-01"]
    )

    assert _is_complete_snapshot(csv_args)
    assert _is_complete_snapshot(api_full_args)
    assert not _is_complete_snapshot(api_partial_args)


def test_response_as_of_becomes_the_next_cursor():
    cursor, warning = _next_cursor("2026-08-06T12:00:00Z")

    assert cursor == "2026-08-06T12:00:00Z"
    assert warning is None


def test_missing_as_of_leaves_the_cursor_unchanged():
    cursor, warning = _next_cursor(None)

    assert cursor is None
    assert warning is not None and "metaData.asOf" in warning


def test_malformed_as_of_leaves_the_cursor_unchanged():
    cursor, warning = _next_cursor("not-a-timestamp")

    assert cursor is None
    assert warning is not None and "invalid cursor timestamp" in warning


def test_future_as_of_leaves_the_cursor_unchanged():
    cursor, warning = _next_cursor("2999-01-01T00:00:00Z")

    assert cursor is None
    assert warning is not None and "too far in the future" in warning


def test_as_of_that_predates_the_held_cursor_is_refused():
    """A stale replica must not drag the watermark backwards.

    Without this the sync never converges: each rewind makes the next run
    re-request a wider window, which the same stale replica rewinds again.
    """
    cursor, warning = _next_cursor("2026-08-01T00:00:00Z", "2026-08-06T12:00:00Z")

    assert cursor is None
    assert warning is not None and "predates the cursor" in warning


def test_as_of_equal_to_the_held_cursor_is_accepted():
    """Standing still is not moving backwards."""
    cursor, warning = _next_cursor("2026-08-06T12:00:00Z", "2026-08-06T12:00:00Z")

    assert cursor == "2026-08-06T12:00:00Z"
    assert warning is None


def test_as_of_newer_than_the_held_cursor_advances():
    cursor, warning = _next_cursor("2026-08-06T12:00:00Z", "2026-08-01T00:00:00Z")

    assert cursor == "2026-08-06T12:00:00Z"
    assert warning is None


def test_unusable_floor_cannot_veto_a_valid_as_of():
    """A floor that cannot be ordered cannot outrank anything."""
    cursor, warning = _next_cursor("2026-08-06T12:00:00Z", "not-a-timestamp")

    assert cursor == "2026-08-06T12:00:00Z"
    assert warning is None


def test_ingest_records_failed_run_for_missing_csv(tmp_path):
    db = tmp_path / "review.sqlite"
    missing = tmp_path / "missing.csv"
    args = build_parser().parse_args(["ingest", "--source", "csv", str(missing), "--db", str(db)])

    assert args.func(args) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT state FROM runs").fetchone()[0] == RUN_FAILED


def test_model_key_is_loaded_from_the_age_vault_when_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = []

    def fake_load_into_env(*, required, verbose):
        calls.append((required, verbose))

    monkeypatch.setattr("simplifi_runtime.secrets.load_into_env", fake_load_into_env)

    _ensure_model_key("luna", verbose=True)

    assert calls == [(["OPENAI_API_KEY"], True)]


def test_probe_health_reports_status_code_and_stale_refresh():
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    health = _aggregator_health(
        {
            "name": "Example Bank",
            "aggregators": [
                {
                    "aggStatus": "ERROR",
                    "aggStatusCode": "FDP-192",
                    "aggStatusDetail": "unsupported",
                    "lastRefreshSuccessfulAt": "2026-07-01T00:00:00Z",
                }
            ],
        },
        now=now,
        expected_refresh_days=14,
    )

    assert health[0]["issues"] == [
        "status is not OK",
        "care code present",
        "last successful refresh is stale",
    ]


def test_probe_health_without_expected_cadence_keeps_age_informational():
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    health = _aggregator_health(
        {
            "name": "Example Bank",
            "aggregators": [
                {
                    "aggStatus": "OK",
                    "lastRefreshSuccessfulAt": "2026-07-01T00:00:00Z",
                }
            ],
        },
        now=now,
    )

    assert health[0]["issues"] == []
    assert health[0]["age_days"] > 14


def test_latest_run_ignores_failed_runs(tmp_path):
    store = Store(tmp_path / "review.sqlite")
    successful = store.start_run("csv", "good")
    store.finish_run(successful, RUN_SUCCEEDED, 1)
    failed = store.start_run("api", "bad")
    store.finish_run(failed, RUN_FAILED, 0)
    store.commit()
    store.close()

    assert _latest_run(tmp_path / "review.sqlite") == (successful, "csv")


def test_api_missing_exclusion_state_is_visible_as_a_report_limitation():
    limitations = _analysis_limitations("api", [{"exclusion_flag": 2}])

    assert len(limitations) == 1
    assert "isExcludedFromReports" in limitations[0]
    assert "not evidence of a clean review" in limitations[0]


def test_csv_missing_settlement_state_is_visible_without_erasing_review_rows():
    limitations = _analysis_limitations(
        "csv",
        [
            {
                "exclusion_flag": 0,
                "review_eligible": 1,
                "eligibility_reason_codes": "missing_optional_field,eligible",
            }
        ],
    )

    assert len(limitations) == 1
    assert "1 eligible row(s) remain visible for general review" in limitations[0]
    assert "require explicit CLEARED state" in limitations[0]


def test_csv_limitation_excludes_ineligible_rows_from_visible_count():
    limitations = _analysis_limitations(
        "csv",
        [
            {
                "exclusion_flag": 0,
                "review_eligible": 1,
                "eligibility_reason_codes": "missing_optional_field,eligible",
            },
            {
                "exclusion_flag": 1,
                "review_eligible": 0,
                "eligibility_reason_codes": "excluded_from_reports,missing_optional_field",
            },
        ],
    )

    assert "1 eligible row(s) remain visible for general review" in limitations[0]


def test_api_missing_settlement_state_is_reported():
    limitations = _analysis_limitations(
        "api",
        [
            {
                "exclusion_flag": 0,
                "review_eligible": 1,
                "eligibility_reason_codes": "missing_optional_field,eligible",
            }
        ],
    )

    assert any("1 eligible row(s)" in limitation for limitation in limitations)
    assert any("lack a confirmed CLEARED state" in limitation for limitation in limitations)


def test_model_taxonomy_excludes_non_spending_and_unsettled_rows():
    rows = [
        {
            "account_name": "Checking",
            "category": "Groceries",
            "is_uncategorized": 0,
            "poisons_statistics": 0,
            "txn_state": "CLEARED",
        },
        {
            "account_name": "Checking",
            "category": "Transfer",
            "is_uncategorized": 0,
            "poisons_statistics": 1,
            "txn_state": "CLEARED",
        },
        {
            "account_name": "Checking",
            "category": "Pending Category",
            "is_uncategorized": 0,
            "poisons_statistics": 0,
            "txn_state": "PENDING",
        },
    ]

    assert _model_taxonomy(rows) == ["Groceries"]


def test_known_categories_keep_unsettled_labels_but_drop_transfer_targets():
    rows = [
        {
            "account_name": "Checking",
            "category": "Groceries",
            "is_uncategorized": 0,
            "poisons_statistics": 0,
            "txn_state": "CLEARED",
        },
        {
            "account_name": "Checking",
            "category": "Subscriptions",
            "is_uncategorized": 0,
            "poisons_statistics": 0,
            "txn_state": "",
        },
        {
            "account_name": "Checking",
            "category": "Checking",
            "is_uncategorized": 0,
            "poisons_statistics": 1,
            "txn_state": "CLEARED",
        },
        {
            "account_name": "Checking",
            "category": "",
            "is_uncategorized": 1,
            "poisons_statistics": 0,
            "txn_state": "CLEARED",
        },
    ]

    assert _known_categories(rows) == {"Groceries", "Subscriptions"}
    assert _model_taxonomy(rows) == ["Groceries"]


def test_csv_text_escapes_formula_leading_values():
    for prefix in ("=", "+", "-", "@"):
        assert _csv_safe_text(prefix + "payload") == "'" + prefix + "payload"
    assert _csv_safe_text("Groceries") == "Groceries"
