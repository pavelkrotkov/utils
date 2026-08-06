import argparse
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from simplifi_runtime.cli import (
    _aggregator_health,
    _analysis_limitations,
    _as_of_rows,
    _csv_safe_text,
    _model_taxonomy,
    build_parser,
)

SKILL_DIR = Path(__file__).resolve().parents[1]
ENTRYPOINT = SKILL_DIR / "scripts" / "simplifi_transaction_reconciler.py"
COMMANDS = {"ingest", "analyze", "classify", "subs", "probe", "schema"}


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
    )

    assert health[0]["issues"] == [
        "status is not OK",
        "care code present",
        "last successful refresh is stale",
    ]


def test_api_missing_exclusion_state_is_visible_as_a_report_limitation():
    limitations = _analysis_limitations("api", [{"exclusion_flag": 2}])

    assert len(limitations) == 1
    assert "isExcludedFromReports" in limitations[0]
    assert "not evidence of a clean review" in limitations[0]


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


def test_csv_text_escapes_formula_leading_values():
    for prefix in ("=", "+", "-", "@"):
        assert _csv_safe_text(prefix + "payload") == "'" + prefix + "payload"
    assert _csv_safe_text("Groceries") == "Groceries"
