import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

from simplifi_runtime.cli import _as_of_rows, build_parser

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
