"""Safeguards for runs nobody is watching."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from simplifi_runtime import artifacts, unattended
from simplifi_runtime.cli import build_parser
from simplifi_runtime.store import RUN_FAILED, RUN_STARTED, RUN_SUCCEEDED, Store

FIXTURE_CSV = Path(__file__).parent / "fixtures" / "acceptance.csv"


def run(arguments: list[str]) -> int:
    args = build_parser().parse_args(arguments)
    return args.func(args)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    monkeypatch.setenv(artifacts.DATA_DIR_ENV, str(directory))
    return directory


@pytest.fixture
def ingested(data_dir):
    assert run(["ingest", "--source", "csv", str(FIXTURE_CSV)]) == 0
    return data_dir


# --- refusing an unsafe scheduled configuration -----------------------------


def test_an_unattended_run_requires_a_stated_data_directory():
    with pytest.raises(unattended.UnattendedError, match="no data directory was stated"):
        unattended.assert_unattended_safe(
            data_dir_is_explicit=False, allow_unsafe_paths=False, sends_to_model=False
        )


def test_an_unattended_run_refuses_relaxed_path_checks():
    with pytest.raises(unattended.UnattendedError, match="allow-unsafe-paths"):
        unattended.assert_unattended_safe(
            data_dir_is_explicit=True, allow_unsafe_paths=True, sends_to_model=False
        )


def test_an_unattended_run_refuses_model_egress():
    """Sending rests on someone having reviewed the payload; a timer cannot."""
    with pytest.raises(unattended.UnattendedError, match="--send"):
        unattended.assert_unattended_safe(
            data_dir_is_explicit=True, allow_unsafe_paths=False, sends_to_model=True
        )


def test_every_problem_is_reported_at_once():
    """One run, one list of what to fix — not three scheduled failures."""
    with pytest.raises(unattended.UnattendedError) as caught:
        unattended.assert_unattended_safe(
            data_dir_is_explicit=False, allow_unsafe_paths=True, sends_to_model=True
        )

    message = str(caught.value)
    assert "no data directory" in message
    assert "allow-unsafe-paths" in message
    assert "--send" in message


def test_a_safe_configuration_passes():
    unattended.assert_unattended_safe(
        data_dir_is_explicit=True, allow_unsafe_paths=False, sends_to_model=False
    )


def test_the_command_refuses_an_unattended_run_without_a_data_directory(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv(artifacts.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))

    code = run(["ingest", "--source", "csv", str(FIXTURE_CSV), "--unattended"])

    assert code == 2
    assert "no data directory was stated" in capsys.readouterr().err
    assert not (tmp_path / "share").exists()


def test_the_command_accepts_an_unattended_run_with_one(data_dir):
    assert run(["ingest", "--source", "csv", str(FIXTURE_CSV), "--unattended"]) == 0


def test_an_unattended_classify_cannot_send(ingested, capsys):
    code = run(["classify", "--unattended", "--send"])

    assert code == 2
    assert "--send" in capsys.readouterr().err


def test_an_unattended_classify_still_writes_its_payload_locally(ingested):
    assert run(["classify", "--unattended"]) == 0


# --- unattended runs never mutate -------------------------------------------


def test_no_command_offers_a_mutating_option():
    """The read-only boundary is a property of the interface, not a habit."""
    parser = build_parser()
    forbidden = ("--write", "--apply", "--push", "--commit", "--update-provider", "--delete")
    rendered = parser.format_help()
    for subparser in _subparsers(parser).choices.values():
        rendered += subparser.format_help()

    for option in forbidden:
        assert option not in rendered, option


def test_an_unattended_analyze_leaves_the_transaction_table_untouched(ingested):
    db = ingested / "simplifi.sqlite"
    before = _transaction_versions(db)

    assert run(["analyze", "--unattended", "--today", "2026-06-15"]) == 0

    assert _transaction_versions(db) == before


def _subparsers(parser):
    import argparse

    return next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )


def _transaction_versions(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return list(
            conn.execute(
                "SELECT id, transaction_id, is_current, source_hash FROM transaction_version "
                "ORDER BY id"
            )
        )


# --- re-running the same window is idempotent -------------------------------


def test_re_ingesting_the_same_input_adds_no_versions(ingested):
    """A schedule that fires twice must not double the ledger."""
    db = ingested / "simplifi.sqlite"
    before = _transaction_versions(db)

    assert run(["ingest", "--source", "csv", str(FIXTURE_CSV)]) == 0

    assert _transaction_versions(db) == before


def test_re_running_analyze_produces_the_same_report(ingested):
    assert run(["analyze", "--today", "2026-06-15"]) == 0
    first = (ingested / "report.html").read_text()

    assert run(["analyze", "--today", "2026-06-15"]) == 0
    second = (ingested / "report.html").read_text()

    assert first == second


def test_a_second_ingest_records_its_own_run(ingested):
    """Idempotent in data, not silent: the attempt is still evidence."""
    db = ingested / "simplifi.sqlite"
    assert run(["ingest", "--source", "csv", str(FIXTURE_CSV)]) == 0

    store = Store(db)
    try:
        history = store.run_history(limit=10)
    finally:
        store.close()

    assert len(history) == 2
    assert all(run_row["state"] == RUN_SUCCEEDED for run_row in history)


# --- the cursor only moves on success ---------------------------------------


def test_a_failed_run_does_not_advance_the_cursor(data_dir):
    db = data_dir / "simplifi.sqlite"
    store = Store(db)
    try:
        first = store.start_run("api", "detail", cursor_before=None)
        store.finish_run(first, RUN_SUCCEEDED, 5, cursor_after="2026-06-01T00:00:00Z")
        store.commit()
        before = store.latest_cursor("api")

        second = store.start_run("api", "detail", cursor_before=before)
        store.finish_run(second, RUN_FAILED, 0, cursor_after="2026-07-01T00:00:00Z")
        store.commit()

        assert store.latest_cursor("api") == before
    finally:
        store.close()


def test_an_unfinished_run_does_not_advance_the_cursor(data_dir):
    """A killed process leaves `started`, which must not count as coverage."""
    db = data_dir / "simplifi.sqlite"
    store = Store(db)
    try:
        first = store.start_run("api", "detail")
        store.finish_run(first, RUN_SUCCEEDED, 5, cursor_after="2026-06-01T00:00:00Z")
        store.commit()

        store.start_run("api", "detail", cursor_before="2026-06-01T00:00:00Z")
        store.commit()

        assert store.latest_cursor("api") == "2026-06-01T00:00:00Z"
    finally:
        store.close()


# --- status -----------------------------------------------------------------


def test_status_reports_a_healthy_run(ingested, capsys):
    assert run(["status"]) == 0

    out = capsys.readouterr().out
    assert "OK csv: run 1 succeeded" in out
    assert "scope=" in out


def test_status_fails_when_the_latest_run_failed(ingested, capsys):
    db = ingested / "simplifi.sqlite"
    store = Store(db)
    try:
        run_id = store.start_run("csv", "detail")
        store.finish_run(run_id, RUN_FAILED, 0, error_class="SchemaError", error_message="bad")
        store.commit()
    finally:
        store.close()

    assert run(["status"]) == 1

    captured = capsys.readouterr()
    assert "SchemaError: bad" in captured.out
    assert "did not succeed" in captured.err


def test_status_reports_an_interrupted_run_as_unhealthy(ingested, capsys):
    db = ingested / "simplifi.sqlite"
    store = Store(db)
    try:
        store.start_run("csv", "detail")
        store.commit()
    finally:
        store.close()

    assert run(["status"]) == 1
    assert RUN_STARTED in capsys.readouterr().out


def test_status_without_a_database_is_a_finding(data_dir, capsys):
    """A schedule that never ran looks healthy if you only check for errors."""
    assert run(["status"]) == 2
    assert "no database" in capsys.readouterr().err


def test_status_covers_every_source_not_just_the_newest_run(ingested, capsys):
    db = ingested / "simplifi.sqlite"
    store = Store(db)
    try:
        failed = store.start_run("api", "detail")
        store.finish_run(failed, RUN_FAILED, 0, error_class="ApiError", error_message="401")
        store.commit()
        later = store.start_run("csv", "detail")
        store.finish_run(later, RUN_SUCCEEDED, 7)
        store.commit()
    finally:
        store.close()

    assert run(["status"]) == 1

    out = capsys.readouterr().out
    assert "OK csv" in out
    assert "!! api" in out


def test_status_lists_recent_runs_when_verbose(ingested, capsys):
    assert run(["ingest", "--source", "csv", str(FIXTURE_CSV)]) == 0

    assert run(["status", "--verbose"]) == 0

    assert "Recent runs:" in capsys.readouterr().out


# --- the funnel and its diagnosis -------------------------------------------


def eligible_row(**overrides):
    base = {"eligibility_reason_codes": "eligible", "posted_on": "2026-06-01"}
    base.update(overrides)
    return base


def ineligible_row(reason="excluded_from_reports", **overrides):
    base = {"eligibility_reason_codes": reason, "posted_on": "2026-06-01"}
    base.update(overrides)
    return base


def test_the_funnel_counts_each_stage():
    rows = [eligible_row(), eligible_row(), ineligible_row()]

    funnel = unattended.build_funnel(rows=rows, within_window=rows, scored=rows[:2], findings=1)

    assert funnel.input_rows == 3
    assert funnel.eligible_rows == 2
    assert funnel.analyzed_rows == 2
    assert funnel.discarded_rows == 1
    assert funnel.findings == 1


def test_rows_out_of_window_and_ineligible_are_each_counted():
    rows = [eligible_row(), ineligible_row(), ineligible_row()]

    funnel = unattended.build_funnel(rows=rows, within_window=rows[:2], scored=rows[:1], findings=0)

    assert funnel.analyzed_rows == 1
    assert funnel.discarded_rows == 2
    assert funnel.out_of_window_rows == 1
    assert funnel.ineligible_rows == 2


def test_findings_need_no_diagnosis():
    rows = [eligible_row()]

    funnel = unattended.build_funnel(rows=rows, within_window=rows, scored=rows, findings=1)

    assert funnel.diagnosis() == []


def test_reading_nothing_at_all_is_distinguished_from_a_clean_result():
    funnel = unattended.build_funnel(rows=[], within_window=[], scored=[], findings=0)

    assert "No transactions were read at all" in funnel.diagnosis()[0]
    assert "check `status`" in funnel.diagnosis()[0]


def test_everything_ineligible_is_not_reported_as_a_clean_bill():
    rows = [ineligible_row(), ineligible_row()]

    diagnosis = unattended.build_funnel(
        rows=rows, within_window=rows, scored=[], findings=0
    ).diagnosis()

    assert "ruled ineligible" in diagnosis[0]
    assert "not a clean bill" in diagnosis[0]


def test_everything_out_of_window_says_so():
    rows = [eligible_row(), eligible_row()]

    diagnosis = unattended.build_funnel(
        rows=rows, within_window=[], scored=[], findings=0
    ).diagnosis()

    assert "none survived the analysis date bound" in diagnosis[0]


def test_a_genuinely_clean_result_says_so_plainly():
    rows = [eligible_row(), eligible_row()]

    diagnosis = unattended.build_funnel(
        rows=rows, within_window=rows, scored=rows, findings=0
    ).diagnosis()

    assert "none met a review threshold" in diagnosis[0]


def test_review_eligible_but_unscored_rows_are_not_a_clean_bill():
    """A CSV export is eligible for review and invisible to every analyzer."""
    rows = [eligible_row(), eligible_row()]

    diagnosis = unattended.build_funnel(
        rows=rows, within_window=rows, scored=[], findings=0
    ).diagnosis()

    assert "none were scored by any analyzer" in diagnosis[0]
    assert "source limitation, not a clean bill" in diagnosis[0]
    assert "none met a review threshold" not in "\n".join(diagnosis)


def test_a_partial_clean_result_names_the_rows_it_does_not_cover():
    rows = [eligible_row(), eligible_row(), eligible_row()]

    diagnosis = unattended.build_funnel(
        rows=rows, within_window=rows, scored=rows[:1], findings=0
    ).diagnosis()
    joined = "\n".join(diagnosis)

    assert "none met a review threshold" in joined
    assert "does not cover them" in joined


def test_the_diagnosis_explains_each_reason_code():
    rows = [ineligible_row("unsupported_state"), ineligible_row("missing_required_field")]

    diagnosis = unattended.build_funnel(
        rows=rows, within_window=rows, scored=[], findings=0
    ).diagnosis()
    joined = "\n".join(diagnosis)

    assert "not settled" in joined
    assert "cannot be reasoned about at all" in joined


def test_an_unrecognized_reason_code_is_still_reported():
    """A code we do not have prose for is still evidence."""
    rows = [ineligible_row("some_future_code")]

    joined = "\n".join(
        unattended.build_funnel(rows=rows, within_window=rows, scored=[], findings=0).diagnosis()
    )

    assert "some_future_code" in joined


# --- the report identifies its own inputs -----------------------------------


def test_the_report_names_its_run_source_dataset_and_cursor(ingested):
    assert run(["analyze", "--today", "2026-06-15"]) == 0

    html = (ingested / "report.html").read_text()
    assert "Run identification" in html
    for label in ("Run", "Source", "Dataset", "Cursor from", "Cursor to", "Snapshot"):
        assert f"<td>{label}</td>" in html


def test_an_unscoped_dataset_says_so_rather_than_appearing_blank():
    identity = unattended.RunIdentity(run_id=3, source="csv")

    assert identity.dataset == "unscoped"
    assert ("Dataset", "unscoped") in identity.items()


def test_the_report_carries_the_funnel_counts(ingested):
    assert run(["analyze", "--today", "2026-06-15"]) == 0

    html = (ingested / "report.html").read_text()
    for card in ("Eligible", "Analyzed", "Discarded", "Findings"):
        assert card in html


def test_a_report_with_no_findings_explains_itself(ingested):
    assert run(["analyze", "--today", "2026-06-15"]) == 0

    html = (ingested / "report.html").read_text()
    assert "Why this report has no findings" in html


# --- review findings, each named for the false clean it would have allowed ---


def test_a_csv_report_does_not_claim_its_rows_were_examined(ingested):
    """CSV carries no settlement state, so no analyzer scores any row."""
    assert run(["analyze", "--today", "2026-06-15"]) == 0

    html = (ingested / "report.html").read_text()
    assert "none were scored by any analyzer" in html
    assert "source limitation, not a clean bill" in html
    assert "none met a review threshold" not in html


def test_recurring_findings_count_as_findings():
    """A report listing a price hike must not announce that it found nothing."""
    rows = [eligible_row(), eligible_row()]

    funnel = unattended.build_funnel(rows=rows, within_window=rows, scored=rows, findings=1)

    assert funnel.findings == 1
    assert funnel.diagnosis() == []


def test_status_keeps_each_cursor_scope_separate(ingested, capsys):
    """A later success for one scope must not bury a failure in another."""
    db = ingested / "simplifi.sqlite"
    store = Store(db)
    try:
        broken = store.start_run("api", "profile-a")
        store.record_run_scope(broken, None, "scope-a")
        store.finish_run(broken, RUN_FAILED, 0, error_class="ApiError", error_message="401")
        store.commit()
        healthy = store.start_run("api", "profile-b")
        store.record_run_scope(healthy, None, "scope-b")
        store.finish_run(healthy, RUN_SUCCEEDED, 12, cursor_after="2026-06-01T00:00:00Z")
        store.commit()
    finally:
        store.close()

    assert run(["status"]) == 1

    captured = capsys.readouterr()
    assert "scope=scope-a" in captured.out
    assert "scope=scope-b" in captured.out
    assert "scope scope-a" in captured.err


def test_status_fails_when_a_schedule_stopped_firing(ingested, capsys):
    """Every state stays `succeeded` forever if cron simply stops invoking."""
    db = ingested / "simplifi.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE runs SET started_at = ?, finished_at = ?",
            ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
        )

    assert run(["status", "--max-age-hours", "24"]) == 1

    captured = capsys.readouterr()
    assert "stale: no run since this one" in captured.out
    assert "has not run since" in captured.err


def test_a_recent_run_is_not_stale(ingested):
    assert run(["status", "--max-age-hours", "24"]) == 0


def test_status_says_when_it_cannot_detect_a_stopped_schedule(ingested, capsys):
    assert run(["status"]) == 0

    assert "cannot be distinguished from a healthy one" in capsys.readouterr().out


def test_stale_detection_is_off_without_a_stated_cadence():
    runs = [{"id": 1, "finished_at": "2020-01-01T00:00:00+00:00"}]

    assert unattended.stale_runs(runs, max_age_hours=None) == []


def test_an_unparsable_timestamp_is_not_treated_as_stale():
    """Refusing to guess beats reporting a failure we cannot substantiate."""
    runs = [{"id": 1, "finished_at": "not a date"}]

    assert unattended.stale_runs(runs, max_age_hours=1) == []


def test_a_report_over_two_scopes_says_its_dataset_is_composite():
    """Rows are isolated by source alone, so one scope name would be a lie."""
    identity = unattended.RunIdentity(
        run_id=4, source="api", cursor_scope="scope-b", known_scopes=("scope-a", "scope-b")
    )

    assert "composite of 2 scopes" in identity.dataset
    assert "scope-a" in identity.dataset


def test_a_single_scope_report_names_that_scope():
    identity = unattended.RunIdentity(
        run_id=4, source="api", cursor_scope="scope-a", known_scopes=("scope-a",)
    )

    assert identity.dataset == "scope-a"


def test_the_transactions_card_reconciles_with_the_discard_count(ingested):
    """The card was the post-date-filter count while discards used the input."""
    assert run(["analyze", "--today", "2026-06-15"]) == 0

    html = (ingested / "report.html").read_text()
    assert "Eligible for review" in html
    # 7 fixture rows, all within the window, none settled: nothing is scored.
    assert "Transactions</div><div class=v>7" in html
    assert "Discarded</div><div class=v>7" in html
