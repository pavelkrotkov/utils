"""End-to-end acceptance coverage for source adapters through HTML reports."""

from __future__ import annotations

import json
import signal
import sqlite3
from pathlib import Path

import pytest
from simplifi_runtime import decisions, sync_scope
from simplifi_runtime.cli import _latest_run, build_parser
from simplifi_runtime.sources import api_source
from simplifi_runtime.store import (
    RETIRED_BY_ABSENCE,
    RETIRED_BY_TOMBSTONE,
    RUN_ABORTED,
    RUN_FAILED,
    RUN_SUCCEEDED,
    Store,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


class FixtureApiClient:
    """Small read-only API double backed by the sanitized fixture JSON.

    Carries an identity (profile, dataset, token subject) because the cursor is
    keyed by it; tests vary these to prove two scopes keep separate histories.
    """

    def __init__(
        self,
        payload: dict,
        profile_id: str = "profile-1",
        dataset_id: str = "dataset-1",
        subject: str | None = "subject-1",
    ):
        self.payload = payload
        self.profile_id = profile_id
        self.dataset_id = dataset_id
        self.claims = {"sub": subject} if subject is not None else {}

    def verify(self) -> dict:
        return {"id": self.profile_id}

    def accounts(self) -> list[dict]:
        return self.payload["accounts"]

    def categories(self) -> list[dict]:
        return self.payload["categories"]

    def transactions(
        self, date_on_after: str | None = None, modified_after: str | None = None
    ) -> api_source.PageResult:
        del date_on_after, modified_after
        return api_source.PageResult(self.payload["transactions"], self.payload.get("asOf"))


def _scoped_cursor(db: Path, client: FixtureApiClient, since: str | None = None) -> str | None:
    """The cursor stored for exactly this client's identity and query scope."""
    store = Store(db)
    try:
        return store.latest_cursor("api", sync_scope.api_scope(client, since=since).key())
    finally:
        store.close()


def _scoped_rows(db: Path, source: str, cursor_scope: str | None) -> list[dict]:
    """Current rows for one scope — what a reader of that dataset would see."""
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM transaction_version WHERE source = ? AND cursor_scope IS ? "
                "AND is_current = 1 ORDER BY posted_on, transaction_id",
                (source, cursor_scope),
            )
        ]


def _current_rows(db: Path, source: str) -> list[dict]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM transaction_version "
                "WHERE source = ? AND is_current = 1 ORDER BY posted_on, transaction_id",
                (source,),
            )
        ]


def _run_ingest(arguments: list[str]) -> None:
    args = build_parser().parse_args(arguments)
    assert args.func(args) == 0


def _run_analyze(db: Path, out: Path) -> str:
    args = build_parser().parse_args(
        [
            "analyze",
            "--db",
            str(db),
            "--out",
            str(out),
            "--today",
            "2026-06-15",
        ]
    )
    assert args.func(args) == 0
    return out.read_text(encoding="utf-8")


def _proposals_document(packet: dict, transaction_id: str, **overrides) -> dict:
    document = {
        "document_type": "simplifi.transaction.proposals",
        "schema_version": "1",
        "packet": {
            "analysis_date": packet["run"]["analysis_date"],
            "dataset_hash": packet["source"]["dataset_hash"],
            "packet_type": packet["packet_type"],
            "run_id": packet["run"]["run_id"],
            "schema_version": packet["schema_version"],
        },
        "reviewer": {"kind": "agent", "id": "acceptance-agent"},
        "proposals": [
            {
                "proposal_id": "proposal-1",
                "transaction_id": transaction_id,
                "decision": "accept",
                "action": "record_category_proposal",
                "category": "Subscriptions",
                "rationale": "Recurring cleared charge matches the established subscription series.",
                "policy_references": ["ADR-004"],
            }
        ],
    }
    document.update(overrides)
    return document


def _run_decide(db: Path, packet_path: Path, proposals: Path, out: Path) -> int:
    args = build_parser().parse_args(
        [
            "decide",
            "--db",
            str(db),
            "--packet",
            str(packet_path),
            "--proposals",
            str(proposals),
            "--out",
            str(out),
        ]
    )
    return args.func(args)


def test_validated_proposals_become_records_without_touching_transactions(tmp_path: Path):
    db = tmp_path / "decide.sqlite"
    report = tmp_path / "review.html"
    _run_ingest(["ingest", "--source", "csv", str(FIXTURE_DIR / "acceptance.csv"), "--db", str(db)])
    _run_analyze(db, report)
    packet_path = report.parent / "review-packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    subscription = next(
        transaction
        for transaction in packet["transactions"]
        if transaction["category"] == "Subscriptions"
    )
    before = _current_rows(db, "csv")

    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps(_proposals_document(packet, subscription["transaction_id"])),
        encoding="utf-8",
    )
    out = tmp_path / "decisions.json"

    assert _run_decide(db, packet_path, proposals, out) == 0

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["document_type"] == "simplifi.transaction.decisions"
    assert written["summary"] == {
        "decision_count": 1,
        "appended_count": 1,
        "already_recorded_count": 0,
    }
    record = written["records"][0]
    assert record["run_id"] == packet["run"]["run_id"]
    assert record["transaction_id"] == subscription["transaction_id"]
    assert record["validator_version"]
    assert record["proposal_hash"]

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        stored = [dict(row) for row in conn.execute("SELECT * FROM decision_record")]
    assert [item["decision_id"] for item in stored] == [record["decision_id"]]
    assert _current_rows(db, "csv") == before, "decisions must not alter transaction state"

    # Recording the same judgment again is a no-op, not a duplicate record.
    assert _run_decide(db, packet_path, proposals, out) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["appended_count"] == 0


def test_decide_rejects_stale_unknown_and_mutating_proposals(tmp_path: Path, capsys):
    db = tmp_path / "reject.sqlite"
    report = tmp_path / "review.html"
    _run_ingest(["ingest", "--source", "csv", str(FIXTURE_DIR / "acceptance.csv"), "--db", str(db)])
    _run_analyze(db, report)
    packet_path = report.parent / "review-packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    transaction_id = packet["transaction_ids"][0]
    proposals = tmp_path / "proposals.json"
    out = tmp_path / "decisions.json"

    mutating = _proposals_document(packet, transaction_id)
    mutating["proposals"][0]["action"] = "apply_category"
    proposals.write_text(json.dumps(mutating), encoding="utf-8")
    assert _run_decide(db, packet_path, proposals, out) == 1
    assert "unsupported_action" in capsys.readouterr().err
    assert not out.exists()

    unknown = _proposals_document(packet, "not-a-transaction")
    proposals.write_text(json.dumps(unknown), encoding="utf-8")
    assert _run_decide(db, packet_path, proposals, out) == 1
    assert "unknown_transaction_id" in capsys.readouterr().err

    # A later successful run supersedes the packet the review was based on.
    _run_ingest(["ingest", "--source", "csv", str(FIXTURE_DIR / "acceptance.csv"), "--db", str(db)])
    proposals.write_text(json.dumps(_proposals_document(packet, transaction_id)), encoding="utf-8")
    assert _run_decide(db, packet_path, proposals, out) == 1
    assert "stale_run_reference" in capsys.readouterr().err

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_record").fetchone()[0] == 0
    assert not out.exists()


def _decide_workspace(tmp_path: Path, name: str, csv_name: str = "acceptance.csv"):
    """Ingest and analyze a fixture, returning the database and its packet."""
    db = tmp_path / f"{name}.sqlite"
    report = tmp_path / f"{name}.html"
    source = tmp_path / f"{name}.csv"
    source.write_text((FIXTURE_DIR / csv_name).read_text(encoding="utf-8"), encoding="utf-8")
    _run_ingest(["ingest", "--source", "csv", str(source), "--db", str(db)])
    _run_analyze(db, report)
    packet_path = tmp_path / f"{name}-packet.json"
    (report.parent / "review-packet.json").rename(packet_path)
    return db, packet_path, json.loads(packet_path.read_text(encoding="utf-8"))


def test_a_packet_from_another_database_cannot_record_decisions(tmp_path: Path, capsys):
    """A shared run ID is not identity: the dataset must match the database."""
    _, packet_path, packet = _decide_workspace(tmp_path, "first")
    other_csv = tmp_path / "other.csv"
    rows = (FIXTURE_DIR / "acceptance.csv").read_text(encoding="utf-8").replace("-10.00", "-13.00")
    other_csv.write_text(rows, encoding="utf-8")
    other_db = tmp_path / "other.sqlite"
    _run_ingest(["ingest", "--source", "csv", str(other_csv), "--db", str(other_db)])
    _run_analyze(other_db, tmp_path / "other.html")

    subscription = next(
        item for item in packet["transactions"] if item["category"] == "Subscriptions"
    )
    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps(_proposals_document(packet, subscription["transaction_id"])), encoding="utf-8"
    )
    out = tmp_path / "decisions.json"

    # Both databases sit on run 1, so only the dataset hash separates them.
    assert _latest_run(other_db)[0] == packet["run"]["run_id"]
    assert _run_decide(other_db, packet_path, proposals, out) == 1
    assert "does not describe this database" in capsys.readouterr().err
    assert not out.exists()
    with sqlite3.connect(other_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_record").fetchone()[0] == 0


def test_reruns_export_the_stored_record_not_a_fresh_timestamp(tmp_path: Path, monkeypatch):
    db, packet_path, packet = _decide_workspace(tmp_path, "rerun")
    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps(_proposals_document(packet, packet["transaction_ids"][0])), encoding="utf-8"
    )
    out = tmp_path / "decisions.json"

    monkeypatch.setattr(decisions, "_now", lambda: "2026-08-16T09:00:00+00:00")
    assert _run_decide(db, packet_path, proposals, out) == 0
    first = json.loads(out.read_text(encoding="utf-8"))["records"][0]

    # The clock has moved on, but the stored judgment has not.
    monkeypatch.setattr(decisions, "_now", lambda: "2026-09-01T17:45:00+00:00")
    assert _run_decide(db, packet_path, proposals, out) == 0
    second = json.loads(out.read_text(encoding="utf-8"))

    assert first["recorded_at"] == "2026-08-16T09:00:00+00:00"

    assert second["summary"]["appended_count"] == 0
    assert second["records"][0] == first, "an exported record must match the stored one"
    with sqlite3.connect(db) as conn:
        stored = conn.execute("SELECT recorded_at FROM decision_record").fetchall()
    assert [row[0] for row in stored] == [first["recorded_at"]]


def test_an_unwritable_output_records_nothing(tmp_path: Path):
    db, packet_path, packet = _decide_workspace(tmp_path, "unwritable")
    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps(_proposals_document(packet, packet["transaction_ids"][0])), encoding="utf-8"
    )
    blocked = tmp_path / "blocked"
    blocked.mkdir()

    # A directory cannot be replaced by the artifact, so it fails before commit.
    assert _run_decide(db, packet_path, proposals, blocked) == 2

    unwritable_parent = tmp_path / "readonly"
    unwritable_parent.mkdir(mode=0o500)
    try:
        args = build_parser().parse_args(
            [
                "decide",
                "--db",
                str(db),
                "--packet",
                str(packet_path),
                "--proposals",
                str(proposals),
                "--out",
                str(unwritable_parent / "nested" / "decisions.json"),
            ]
        )
        with pytest.raises(OSError):
            args.func(args)
    finally:
        unwritable_parent.chmod(0o700)

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_record").fetchone()[0] == 0


def test_an_ingest_racing_the_write_lock_fails_closed(tmp_path: Path, monkeypatch, capsys):
    db, packet_path, packet = _decide_workspace(tmp_path, "race")
    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps(_proposals_document(packet, packet["transaction_ids"][0])), encoding="utf-8"
    )
    out = tmp_path / "decisions.json"
    original = Store.begin_immediate

    def racing_begin(self):
        """Land a successful ingest between the staleness read and the lock."""
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO runs (started_at, source, source_detail, algorithm_version,"
                " ruleset_version, state, row_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2026-06-16T00:00:00+00:00", "csv", "racing", "0.1.0", "0.2.0", RUN_SUCCEEDED, 1),
            )
        monkeypatch.setattr(Store, "begin_immediate", original)
        original(self)

    monkeypatch.setattr(Store, "begin_immediate", racing_begin)

    assert _run_decide(db, packet_path, proposals, out) == 1
    assert "superseded by a concurrent ingest" in capsys.readouterr().err
    assert not out.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_record").fetchone()[0] == 0


def test_decide_refuses_to_overwrite_its_inputs(tmp_path: Path):
    db = tmp_path / "collision.sqlite"
    report = tmp_path / "review.html"
    _run_ingest(["ingest", "--source", "csv", str(FIXTURE_DIR / "acceptance.csv"), "--db", str(db)])
    _run_analyze(db, report)
    packet_path = report.parent / "review-packet.json"
    proposals = tmp_path / "proposals.json"
    proposals.write_text("{}", encoding="utf-8")
    packet_bytes = packet_path.read_bytes()

    for collision in (packet_path, proposals, db):
        assert _run_decide(db, packet_path, proposals, collision) == 2

    assert packet_path.read_bytes() == packet_bytes
    assert db.read_bytes().startswith(b"SQLite format 3\x00")


def test_csv_fixture_reaches_store_and_report_without_false_clean(tmp_path: Path):
    db = tmp_path / "csv.sqlite"
    report = tmp_path / "csv.html"

    _run_ingest(
        [
            "ingest",
            "--source",
            "csv",
            str(FIXTURE_DIR / "acceptance.csv"),
            "--db",
            str(db),
        ]
    )
    rows = _current_rows(db, "csv")
    html = _run_analyze(db, report)
    packet = json.loads((report.parent / "review-packet.json").read_text(encoding="utf-8"))

    assert rows
    assert packet["schema_version"] == "1"
    assert packet["source"]["kind"] == "csv"
    assert packet["summary"]["eligible_transaction_count"] > 0
    assert packet["transaction_ids"]
    assert packet["examples"]
    assert all(example["id"].startswith("judgment-") for example in packet["examples"])
    assert sum(row["review_eligible"] for row in rows) > 0
    excluded = next(
        (row for row in rows if row["payee_display"] == "Ignored Purchase"),
        None,
    )
    assert excluded is not None, "Expected row with 'Ignored Purchase' payee not found"
    assert excluded["review_eligible"] == 0
    assert "excluded_from_reports" in excluded["eligibility_reason_codes"]
    assert "CSV has no settlement or projection metadata" in html
    assert "Transactions</div><div class=v>7" in html


def test_api_fixture_reaches_report_with_review_uncategorized_and_recurring_findings(
    tmp_path: Path, monkeypatch
):
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(
        api_source,
        "client_from_env_or_age",
        lambda verbose=False: FixtureApiClient(payload),
    )
    db = tmp_path / "api.sqlite"
    report = tmp_path / "api.html"

    _run_ingest(["ingest", "--source", "api", "--full-rescan", "--db", str(db)])
    rows = _current_rows(db, "api")
    html = _run_analyze(db, report)
    packet = json.loads((report.parent / "review-packet.json").read_text(encoding="utf-8"))

    assert rows
    assert packet["schema_version"] == "1"
    assert packet["source"]["kind"] == "api"
    assert packet["findings"]
    assert packet["examples"]
    assert any(
        example["title"] == "Projected versus real subscription" for example in packet["examples"]
    )
    assert all("payee_raw" not in transaction for transaction in packet["transactions"])
    assert sum(row["review_eligible"] for row in rows) > 0
    assert "subscription_creep" in html
    assert "MYSTERY PURCHASE" in html

    # The run persists the response's asOf, not the newest modifiedAt among the
    # rows (2026-05-18T12:00:00Z in this fixture).
    with sqlite3.connect(db) as conn:
        cursor_after = conn.execute(
            "SELECT cursor_after FROM runs WHERE outcome = 'success' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert cursor_after == "2026-06-01T00:00:00Z"
    assert "LIMITATION: The API bulk transaction response did not expose" in html
    assert "Excluded from stats</div><div class=v>3" in html

    excluded = next(
        (row for row in rows if row["transaction_id"] == "api-excluded"),
        None,
    )
    assert excluded is not None, "Expected row with 'api-excluded' transaction_id not found"
    assert excluded["review_eligible"] == 0
    assert "excluded_from_reports" in excluded["eligibility_reason_codes"]

    transfer = next(
        (row for row in rows if row["transaction_id"] == "api-transfer"),
        None,
    )
    assert transfer is not None, "Expected row with 'api-transfer' transaction_id not found"
    assert transfer["kind"] == "transfer"
    assert transfer["poisons_statistics"] == 1
    assert "category matches an account name" in transfer["semantics_reasons"]

    unknown = next(
        (row for row in rows if row["transaction_id"] == "api-unknown-exclusion"),
        None,
    )
    assert unknown is not None, "Expected row with 'api-unknown-exclusion' transaction_id not found"
    assert unknown["review_eligible"] == 1
    assert unknown["exclusion_flag"] == 2
    assert "report_exclusion_unknown" in unknown["eligibility_reason_codes"]


def test_analyze_rejects_report_and_packet_path_collision(tmp_path: Path):
    db = tmp_path / "collision.sqlite"
    output = tmp_path / "review.html"
    _run_ingest(
        [
            "ingest",
            "--source",
            "csv",
            str(FIXTURE_DIR / "acceptance.csv"),
            "--db",
            str(db),
        ]
    )

    args = build_parser().parse_args(
        [
            "analyze",
            "--db",
            str(db),
            "--out",
            str(output),
            "--packet-out",
            str(output),
            "--today",
            "2026-06-15",
        ]
    )

    assert args.func(args) == 2
    assert not output.exists()

    db_collision_output = tmp_path / "db-collision.html"
    db_collision_args = build_parser().parse_args(
        [
            "analyze",
            "--db",
            str(db),
            "--out",
            str(db_collision_output),
            "--packet-out",
            str(db),
            "--today",
            "2026-06-15",
        ]
    )

    assert db_collision_args.func(db_collision_args) == 2
    assert db.read_bytes().startswith(b"SQLite format 3\x00")


def test_api_run_without_as_of_does_not_advance_the_cursor(tmp_path: Path, monkeypatch):
    """A run that ingests fine but yields no marker must leave the cursor alone.

    The rows are still stored — refusing them would discard good data over a
    metadata defect — but the next run re-requests the same window rather than
    trusting a boundary the server never stated.
    """
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    payload.pop("asOf")
    monkeypatch.setattr(
        api_source,
        "client_from_env_or_age",
        lambda verbose=False: FixtureApiClient(payload),
    )
    db = tmp_path / "api.sqlite"

    _run_ingest(["ingest", "--source", "api", "--full-rescan", "--db", str(db)])

    assert _current_rows(db, "api")
    assert _scoped_cursor(db, FixtureApiClient(payload)) is None


def test_stale_as_of_does_not_rewind_an_earned_cursor(tmp_path: Path, monkeypatch):
    """A later run reading a stale replica must not walk the watermark back.

    The second run is a full rescan, which sends no modifiedAfter — so the
    floor has to come from the stored watermark, not from the request.
    """
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    client = FixtureApiClient(payload)
    monkeypatch.setattr(api_source, "client_from_env_or_age", lambda verbose=False: client)
    db = tmp_path / "api.sqlite"
    _run_ingest(["ingest", "--source", "api", "--full-rescan", "--db", str(db)])

    assert _scoped_cursor(db, client) == "2026-06-01T00:00:00Z"

    payload["asOf"] = "2026-05-01T00:00:00Z"
    _run_ingest(["ingest", "--source", "api", "--full-rescan", "--db", str(db)])

    assert _scoped_cursor(db, client) == "2026-06-01T00:00:00Z"


def _ingest_as(monkeypatch, client, db: Path, *extra: str) -> None:
    monkeypatch.setattr(api_source, "client_from_env_or_age", lambda verbose=False: client)
    _run_ingest(["ingest", "--source", "api", "--db", str(db), *extra])


@pytest.mark.parametrize(
    ("field", "value"),
    [("dataset_id", "dataset-2"), ("profile_id", "profile-2"), ("subject", "subject-2")],
    ids=["dataset", "profile", "auth"],
)
def test_each_identity_component_keeps_its_own_cursor_history(
    tmp_path: Path, monkeypatch, field, value
):
    """A second identity must not inherit the first one's high-water mark.

    Inheriting it is silent data loss: the second scope would request only what
    changed after a mark earned against data it has never read, and everything
    older would never be fetched. The run would still report success.
    """
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    first = FixtureApiClient(payload)
    db = tmp_path / "api.sqlite"
    _ingest_as(monkeypatch, first, db, "--full-rescan")

    assert _scoped_cursor(db, first) == "2026-06-01T00:00:00Z"

    second = FixtureApiClient(payload, **{field: value})
    assert _scoped_cursor(db, second) is None, "a different identity must start with no cursor"

    _ingest_as(monkeypatch, second, db, "--full-rescan")

    # Both histories exist and are independent.
    assert _scoped_cursor(db, first) == "2026-06-01T00:00:00Z"
    assert _scoped_cursor(db, second) == "2026-06-01T00:00:00Z"
    with sqlite3.connect(db) as conn:
        scopes = {row[0] for row in conn.execute("SELECT cursor_scope FROM runs")}
    assert len(scopes) == 2


def test_changing_the_since_scope_starts_a_separate_history(tmp_path: Path, monkeypatch):
    """Widening --since must not be answered from a narrower window's cursor.

    The inherited mark already sits past the newly requested history, so the
    wider window would return nothing new and the run would look complete.
    """
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    client = FixtureApiClient(payload)
    db = tmp_path / "api.sqlite"
    _ingest_as(monkeypatch, client, db, "--since", "2026-05-01")

    assert _scoped_cursor(db, client, since="2026-05-01") == "2026-06-01T00:00:00Z"
    assert _scoped_cursor(db, client, since="2026-01-01") is None
    assert _scoped_cursor(db, client) is None, "an unbounded run is its own scope"

    _ingest_as(monkeypatch, client, db, "--since", "2026-01-01")

    assert _scoped_cursor(db, client, since="2026-05-01") == "2026-06-01T00:00:00Z"
    assert _scoped_cursor(db, client, since="2026-01-01") == "2026-06-01T00:00:00Z"


def test_incremental_run_reuses_only_its_own_scoped_cursor(tmp_path: Path, monkeypatch, capsys):
    """The second run of a scope sends the cursor the first one earned."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    client = FixtureApiClient(payload)
    db = tmp_path / "api.sqlite"
    _ingest_as(monkeypatch, client, db, "--full-rescan")
    capsys.readouterr()

    _ingest_as(monkeypatch, client, db)

    assert "requested=2026-06-01T00:00:00Z" in capsys.readouterr().out

    # A different dataset under the same database gets no cursor at all.
    other = FixtureApiClient(payload, dataset_id="dataset-2")
    _ingest_as(monkeypatch, other, db)

    assert "requested=none" in capsys.readouterr().out


def test_legacy_unscoped_cursor_is_not_adopted_and_is_explained(
    tmp_path: Path, monkeypatch, capsys
):
    """A pre-scoping cursor cannot be attributed, so it is not reused.

    Adopting it would apply a mark of unknown provenance to a known scope —
    exactly the confusion scoping exists to prevent. One wider fetch is the
    price, and the operator is told why rather than left to wonder.
    """
    db = tmp_path / "api.sqlite"
    store = Store(db)
    legacy = store.start_run("api", "api legacy run")
    store.finish_run(legacy, RUN_SUCCEEDED, 1, cursor_after="2026-07-01T00:00:00Z")
    store.commit()
    store.close()

    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    client = FixtureApiClient(payload)
    _ingest_as(monkeypatch, client, db)
    captured = capsys.readouterr()

    assert "cursor from before cursor scoping" in captured.err
    assert "requested=none" in captured.out
    assert _scoped_cursor(db, client) == "2026-06-01T00:00:00Z"


def test_cursor_scope_is_recorded_and_reported(tmp_path: Path, monkeypatch, capsys):
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    client = FixtureApiClient(payload)
    db = tmp_path / "api.sqlite"
    _ingest_as(monkeypatch, client, db, "--since", "2026-05-01")
    out = capsys.readouterr().out

    assert "INFO api cursor scope: source=api profile=" in out
    assert "since=2026-05-01" in out

    with sqlite3.connect(db) as conn:
        scope, detail = conn.execute(
            "SELECT cursor_scope, source_detail FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert json.loads(scope) == json.loads(sync_scope.api_scope(client, since="2026-05-01").key())
    assert "scope=" in detail


def test_two_datasets_keep_independent_current_rows(tmp_path: Path, monkeypatch, capsys):
    """The reported bug, from the other end: B's rescan must not touch A's rows.

    Before state was scoped, current rows were isolated by `source` alone, so
    B's complete rescan retired every row of A's — they were absent from B's
    observed-ID set and nothing distinguished them. Migration 011 answered that
    by making A refuse its own cursor and re-read its whole window, which cost
    a full fetch every time the two alternated. Scoped state removes the
    collision instead of surviving it.
    """
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    first = FixtureApiClient(payload)
    second = FixtureApiClient(payload, dataset_id="dataset-2")
    db = tmp_path / "api.sqlite"
    scope_one = sync_scope.api_scope(first).key()
    scope_two = sync_scope.api_scope(second).key()
    assert scope_one != scope_two

    _ingest_as(monkeypatch, first, db, "--full-rescan")
    seeded = {row["transaction_id"] for row in _scoped_rows(db, "api", scope_one)}
    assert seeded, "the fixture must materialize something to be robbed of"

    _ingest_as(monkeypatch, second, db, "--full-rescan")
    capsys.readouterr()

    # A's rows survive B's complete rescan, and both scopes hold the same set
    # independently rather than sharing one.
    assert {row["transaction_id"] for row in _scoped_rows(db, "api", scope_one)} == seeded
    assert {row["transaction_id"] for row in _scoped_rows(db, "api", scope_two)} == seeded
    store = Store(db)
    try:
        assert store.retired_transaction_ids("api", scope_one) == set()
        assert sorted(store.current_scopes("api")) == sorted([scope_one, scope_two])
    finally:
        store.close()

    # And A resumes incrementally, because its cursor still describes its rows.
    _ingest_as(monkeypatch, first, db)
    captured = capsys.readouterr()
    assert "requested=2026-06-01T00:00:00Z" in captured.out
    assert "re-reads its full window" not in captured.err
    assert {row["transaction_id"] for row in _scoped_rows(db, "api", scope_one)} == seeded
    assert {row["transaction_id"] for row in _scoped_rows(db, "api", scope_two)} == seeded


def test_an_incremental_run_does_not_upsert_into_another_scope(tmp_path: Path, monkeypatch, capsys):
    """The quieter half of the bug: a shared current set, not a wiped one."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    first = FixtureApiClient(payload)
    db = tmp_path / "api.sqlite"
    scope_one = sync_scope.api_scope(first).key()
    _ingest_as(monkeypatch, first, db, "--full-rescan")
    before = {row["transaction_id"] for row in _scoped_rows(db, "api", scope_one)}

    # A second dataset whose transactions are entirely its own.
    other = json.loads(json.dumps(payload))
    for index, transaction in enumerate(other["transactions"]):
        transaction["id"] = f"dataset-2-{index}"
    second = FixtureApiClient(other, dataset_id="dataset-2")
    scope_two = sync_scope.api_scope(second).key()
    _ingest_as(monkeypatch, second, db)
    capsys.readouterr()

    assert {row["transaction_id"] for row in _scoped_rows(db, "api", scope_one)} == before
    assert all(
        row["transaction_id"].startswith("dataset-2-") for row in _scoped_rows(db, "api", scope_two)
    )


def test_analysis_says_when_the_database_holds_other_scopes(tmp_path: Path, monkeypatch, capsys):
    """A scoped report is silent about its siblings, and silence reads as absence."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"
    _ingest_as(monkeypatch, FixtureApiClient(payload), db, "--full-rescan")
    _ingest_as(monkeypatch, FixtureApiClient(payload, dataset_id="dataset-2"), db, "--full-rescan")
    capsys.readouterr()

    html = _run_analyze(db, tmp_path / "review.html")

    assert "holds current API rows under 2 cursor scopes" in html
    assert "are not missing data" in html


def test_a_single_scope_analysis_says_nothing_about_scopes(tmp_path: Path, monkeypatch, capsys):
    """The ordinary case must not acquire a warning it has no reason to carry."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"
    _ingest_as(monkeypatch, FixtureApiClient(payload), db, "--full-rescan")
    capsys.readouterr()

    html = _run_analyze(db, tmp_path / "review.html")

    assert "cursor scopes" not in html


def test_same_scope_rescan_does_not_invalidate_its_own_cursor(tmp_path: Path, monkeypatch, capsys):
    """The guard must not fire on the ordinary case of one scope in one database."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    client = FixtureApiClient(payload)
    db = tmp_path / "api.sqlite"
    _ingest_as(monkeypatch, client, db, "--full-rescan")
    capsys.readouterr()

    _ingest_as(monkeypatch, client, db)
    captured = capsys.readouterr()

    assert "requested=2026-06-01T00:00:00Z" in captured.out
    assert "different cursor scope" not in captured.err


def test_opaque_token_never_resumes_from_a_stored_cursor(tmp_path: Path, monkeypatch, capsys):
    """Without a subject claim, one principal cannot be told from another."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    client = FixtureApiClient(payload, subject=None)
    db = tmp_path / "api.sqlite"
    _ingest_as(monkeypatch, client, db, "--full-rescan")
    assert _scoped_cursor(db, client) == "2026-06-01T00:00:00Z"
    capsys.readouterr()

    _ingest_as(monkeypatch, client, db)
    captured = capsys.readouterr()

    assert "requested=none" in captured.out
    assert "no stable subject claim" in captured.err


def test_explicit_modified_after_is_not_described_as_a_full_window(
    tmp_path: Path, monkeypatch, capsys
):
    """A run that starts where the operator said must not claim it read everything.

    Telling them older records were recovered when the fetch began at their
    explicit cursor is worse than saying nothing at all.
    """
    db = tmp_path / "api.sqlite"
    store = Store(db)
    legacy = store.start_run("api", "api legacy run")
    store.finish_run(legacy, RUN_SUCCEEDED, 1, cursor_after="2026-07-01T00:00:00Z")
    store.commit()
    store.close()

    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    client = FixtureApiClient(payload)
    _ingest_as(monkeypatch, client, db, "--modified-after", "2026-05-01T00:00:00Z")
    captured = capsys.readouterr()

    assert "requested=2026-05-01T00:00:00Z" in captured.out
    assert "re-reads its full window" not in captured.err


def test_complete_snapshot_ownership_is_recorded(tmp_path: Path, monkeypatch):
    """Kept as run provenance after scoping made it unnecessary as a guard.

    `status` reports it, and it is the record of which runs replaced a
    snapshot; scoped state means nothing reads it to decide anything.
    """
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    client = FixtureApiClient(payload)
    db = tmp_path / "api.sqlite"
    _ingest_as(monkeypatch, client, db, "--full-rescan")
    _ingest_as(monkeypatch, client, db)

    store = Store(db)
    try:
        scopes = store.cursor_scopes("api")
    finally:
        store.close()

    assert scopes == [sync_scope.api_scope(client).key()]
    with sqlite3.connect(db) as conn:
        flags = [row[0] for row in conn.execute("SELECT complete_snapshot FROM runs ORDER BY id")]
    # Only the full rescan replaced the snapshot; the incremental run did not.
    assert flags == [1, 0]


def _run_state(db: Path) -> dict:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return dict(
            conn.execute(
                "SELECT id, state, error_class, error_message, row_count, cursor_after "
                "FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )


class ExplodingApiClient(FixtureApiClient):
    """A client that fails at the point the caller chooses."""

    def __init__(self, payload: dict, error: BaseException):
        super().__init__(payload)
        self.error = error

    def transactions(self, date_on_after=None, modified_after=None):
        raise self.error


def _ingest_expecting_failure(monkeypatch, client, db: Path) -> None:
    monkeypatch.setattr(api_source, "client_from_env_or_age", lambda verbose=False: client)
    args = build_parser().parse_args(["ingest", "--source", "api", "--db", str(db)])
    args.func(args)


def test_api_error_records_a_failed_run_with_its_cause(tmp_path: Path, monkeypatch, capsys):
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"
    error = api_source.ApiError("/transactions returned 500. Usually a missing header")

    _ingest_expecting_failure(monkeypatch, ExplodingApiClient(payload, error), db)
    capsys.readouterr()

    run = _run_state(db)
    assert run["state"] == RUN_FAILED
    assert run["error_class"] == "ApiError"
    # A deliberate error already explains itself; it is stored verbatim.
    assert "returned 500" in run["error_message"]
    assert run["cursor_after"] is None


def test_auth_error_records_its_own_class(tmp_path: Path, monkeypatch, capsys):
    """The class is stored separately so failures can be counted by kind."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"

    _ingest_expecting_failure(
        monkeypatch, ExplodingApiClient(payload, api_source.AuthError("token expired")), db
    )
    capsys.readouterr()

    assert _run_state(db)["error_class"] == "AuthError"


def test_unexpected_exception_still_reaches_a_terminal_state(tmp_path: Path, monkeypatch, capsys):
    """The headline bug: a surprise used to leave the run unfinished forever.

    A schema drift surfaces as something nobody wrote an `except` for, so the
    guard catches BaseException rather than a list of anticipated types.
    """
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"
    client = ExplodingApiClient(payload, KeyError("amount"))

    monkeypatch.setattr(api_source, "client_from_env_or_age", lambda verbose=False: client)
    args = build_parser().parse_args(["ingest", "--source", "api", "--db", str(db)])
    with pytest.raises(KeyError):
        args.func(args)
    capsys.readouterr()

    run = _run_state(db)
    assert run["state"] == RUN_FAILED
    assert run["error_class"] == "KeyError"
    # An unexpected error carries context a bare `KeyError: 'amount'` does not.
    assert "unexpected KeyError" in run["error_message"]
    assert "no longer matches the shape" in run["error_message"]


def test_interruption_is_recorded_as_aborted_not_failed(tmp_path: Path, monkeypatch, capsys):
    """Someone stopping a run and a run breaking call for different responses."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"
    client = ExplodingApiClient(payload, KeyboardInterrupt())

    monkeypatch.setattr(api_source, "client_from_env_or_age", lambda verbose=False: client)
    args = build_parser().parse_args(["ingest", "--source", "api", "--db", str(db)])
    with pytest.raises(KeyboardInterrupt):
        args.func(args)
    capsys.readouterr()

    run = _run_state(db)
    assert run["state"] == RUN_ABORTED
    assert run["error_class"] == "KeyboardInterrupt"
    assert "interrupted before it finished" in run["error_message"]


def test_persistence_failure_leaves_a_failed_run_and_no_rows(tmp_path: Path, monkeypatch, capsys):
    """A write that dies mid-way must roll back and still be recorded."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"
    client = FixtureApiClient(payload)
    monkeypatch.setattr(api_source, "client_from_env_or_age", lambda verbose=False: client)

    calls = {"n": 0}
    original = Store.upsert_version

    def failing_upsert(self, run_id, record):
        calls["n"] += 1
        if calls["n"] > 3:
            raise sqlite3.OperationalError("database or disk is full")
        return original(self, run_id, record)

    monkeypatch.setattr(Store, "upsert_version", failing_upsert)
    args = build_parser().parse_args(["ingest", "--source", "api", "--db", str(db)])
    with pytest.raises(sqlite3.OperationalError):
        args.func(args)
    capsys.readouterr()

    run = _run_state(db)
    assert run["state"] == RUN_FAILED
    assert run["error_class"] == "OperationalError"
    assert run["cursor_after"] is None
    assert _current_rows(db, "api") == [], "partial work must not survive"


def test_schema_error_on_csv_records_a_failed_run(tmp_path: Path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("Nope,Not,A,Simplifi,Export\n1,2,3,4,5\n", encoding="utf-8")
    db = tmp_path / "csv.sqlite"

    args = build_parser().parse_args(["ingest", "--source", "csv", str(bad), "--db", str(db)])

    assert args.func(args) == 1
    capsys.readouterr()
    run = _run_state(db)
    assert run["state"] == RUN_FAILED
    assert run["error_class"] == "SchemaError"


def test_missing_csv_path_records_a_failed_run(tmp_path: Path, capsys):
    db = tmp_path / "csv.sqlite"
    missing = tmp_path / "nope.csv"
    args = build_parser().parse_args(["ingest", "--source", "csv", str(missing), "--db", str(db)])

    assert args.func(args) == 1
    capsys.readouterr()
    run = _run_state(db)
    assert run["state"] == RUN_FAILED
    assert run["error_class"] == "FileNotFoundError"


def test_analysis_refuses_a_database_whose_only_run_failed(tmp_path: Path, monkeypatch, capsys):
    """A failed run must never become analysis input, even implicitly."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"
    error = api_source.ApiError("/transactions returned 500")
    _ingest_expecting_failure(monkeypatch, ExplodingApiClient(payload, error), db)
    capsys.readouterr()

    args = build_parser().parse_args(
        ["analyze", "--db", str(db), "--out", str(tmp_path / "r.html")]
    )

    assert args.func(args) == 1
    err = capsys.readouterr().err
    assert "failed" in err
    assert "returned 500" in err, "the recorded cause should reach the operator"


def test_analysis_refuses_an_unfinished_run(tmp_path: Path, capsys):
    """A run left 'started' by a killed process is not analysis input."""
    db = tmp_path / "api.sqlite"
    store = Store(db)
    store.start_run("api", "never finished")
    store.commit()
    store.close()

    args = build_parser().parse_args(
        ["analyze", "--db", str(db), "--out", str(tmp_path / "r.html")]
    )

    assert args.func(args) == 1
    assert "has not finished" in capsys.readouterr().err


def test_a_failed_run_does_not_hide_an_earlier_successful_one(tmp_path: Path, monkeypatch, capsys):
    """Analysis falls back to the last good run rather than refusing outright."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"
    good = FixtureApiClient(payload)
    _ingest_as(monkeypatch, good, db, "--full-rescan")

    error = api_source.ApiError("/transactions returned 500")
    _ingest_expecting_failure(monkeypatch, ExplodingApiClient(payload, error), db)
    capsys.readouterr()

    args = build_parser().parse_args(
        ["analyze", "--db", str(db), "--out", str(tmp_path / "r.html")]
    )

    assert args.func(args) == 0
    with sqlite3.connect(db) as conn:
        states = [row[0] for row in conn.execute("SELECT state FROM runs ORDER BY id")]
    assert states == [RUN_SUCCEEDED, RUN_FAILED]


def test_bookkeeping_failure_does_not_mask_the_original_error(tmp_path: Path, monkeypatch, capsys):
    """Recording the failure must not replace the failure worth reading.

    The database is one of the things that may be broken when this path runs,
    so a SQLite error three frames from its cause must not become the story.
    """
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"
    original = api_source.ApiError("/transactions returned 500")
    client = ExplodingApiClient(payload, original)
    monkeypatch.setattr(api_source, "client_from_env_or_age", lambda verbose=False: client)

    def broken_finish(*_args, **_kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(Store, "finish_run", broken_finish)
    args = build_parser().parse_args(["ingest", "--source", "api", "--db", str(db)])

    # The ApiError is an expected failure, so it still returns 1 rather than
    # raising — and the bookkeeping problem is reported, not swallowed.
    assert args.func(args) == 1
    err = capsys.readouterr().err
    assert "/transactions returned 500" in err
    assert "could not record run" in err
    assert "readonly database" in err


def test_read_commands_migrate_a_legacy_database(tmp_path: Path, monkeypatch, capsys):
    """`analyze` must be able to be the first command run after an upgrade.

    Querying `runs.state` through a raw connection ran before any migration
    could add the column, so a read-only command failed with `no such column`
    — an error about our own schema, shown to someone who did nothing wrong.
    """
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "legacy.sqlite"
    _ingest_as(monkeypatch, FixtureApiClient(payload), db, "--full-rescan")
    capsys.readouterr()

    # Rewind the database to the pre-lifecycle schema, as an upgrade would find it.
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE name = '012_run_lifecycle.sql'")
        conn.execute("DROP INDEX IF EXISTS idx_runs_state")
        for column in ("state", "error_class", "error_message"):
            conn.execute(f"ALTER TABLE runs DROP COLUMN {column}")

    args = build_parser().parse_args(
        ["analyze", "--db", str(db), "--out", str(tmp_path / "r.html"), "--today", "2026-06-15"]
    )

    assert args.func(args) == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT state FROM runs ORDER BY id").fetchone()[0] == RUN_SUCCEEDED


def test_an_error_after_commit_does_not_unmake_a_successful_run(
    tmp_path: Path, monkeypatch, capsys
):
    """A broken pipe while printing must not rewrite a committed run as failed.

    The rollback cannot take back committed rows, so flipping the run would
    leave current transaction rows beside a run that claims it failed —
    analysis would then reject complete data.
    """
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"
    client = FixtureApiClient(payload)
    monkeypatch.setattr(api_source, "client_from_env_or_age", lambda verbose=False: client)

    real_print = print
    calls = {"n": 0}

    def flaky_print(*print_args, **print_kwargs):
        calls["n"] += 1
        # Fail once the ingest has committed and is reporting its summary.
        if calls["n"] > 1 and print_kwargs.get("file") is None:
            raise BrokenPipeError("broken pipe")
        real_print(*print_args, **print_kwargs)

    monkeypatch.setattr("builtins.print", flaky_print)
    args = build_parser().parse_args(["ingest", "--source", "api", "--db", str(db)])
    with pytest.raises(BrokenPipeError):
        args.func(args)
    monkeypatch.undo()
    capsys.readouterr()

    run = _run_state(db)
    assert run["state"] == RUN_SUCCEEDED, "a committed run must stay succeeded"
    assert run["error_class"] is None
    assert _current_rows(db, "api"), "its rows are legitimately current"


def test_sigterm_is_recorded_as_aborted(tmp_path: Path, monkeypatch, capsys):
    """SIGTERM ends a scheduled run far more often than Ctrl-C does.

    Its default action terminates the process outright, so without an
    installed handler the run sits at 'started' forever, indistinguishable
    from one still in flight.
    """
    from simplifi_runtime import cli

    assert cli.install_termination_handler()
    # The installed handler is ours, so invoking it below is the same code path
    # the kernel would enter on delivery.
    assert signal.getsignal(signal.SIGTERM) is cli._raise_system_exit

    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"

    class TerminatedClient(FixtureApiClient):
        def transactions(self, date_on_after=None, modified_after=None):
            cli._raise_system_exit(signal.SIGTERM, None)  # what delivery does, minus the timing
            raise AssertionError("SIGTERM handler must raise")

    monkeypatch.setattr(
        api_source, "client_from_env_or_age", lambda verbose=False: TerminatedClient(payload)
    )
    args = build_parser().parse_args(["ingest", "--source", "api", "--db", str(db)])
    with pytest.raises(SystemExit):
        args.func(args)
    capsys.readouterr()

    run = _run_state(db)
    assert run["state"] == RUN_ABORTED
    assert run["error_class"] == "SystemExit"


def test_tombstoned_and_absent_transactions_are_recorded_distinctly(
    tmp_path: Path, monkeypatch, capsys
):
    """End to end: the two reasons survive an ingest and stay separable."""
    payload = json.loads((FIXTURE_DIR / "acceptance_api.json").read_text(encoding="utf-8"))
    db = tmp_path / "api.sqlite"
    client = FixtureApiClient(payload)
    _ingest_as(monkeypatch, client, db, "--full-rescan")
    seeded = {row["transaction_id"] for row in _current_rows(db, "api")}
    capsys.readouterr()

    # One transaction the provider explicitly deletes, one that simply stops
    # appearing in an otherwise complete scan.
    tombstoned, absent, *_ = sorted(seeded)
    surviving = [t for t in payload["transactions"] if t["id"] not in {tombstoned, absent}]
    payload["transactions"] = [
        *surviving,
        {"id": tombstoned, "isDeleted": True, "modifiedAt": "2026-06-02T00:00:00Z"},
    ]
    _ingest_as(monkeypatch, client, db, "--full-rescan")
    out = capsys.readouterr().out

    assert "INFO retired 1 by provider tombstone, 1 absent from a complete scan" in out

    store = Store(db)
    try:
        by_id = {item["transaction_id"]: item for item in store.retirements(source="api")}
        assert by_id[tombstoned]["reason"] == RETIRED_BY_TOMBSTONE
        assert by_id[absent]["reason"] == RETIRED_BY_ABSENCE
        scope = sync_scope.api_scope(client).key()
        assert store.retired_transaction_ids("api", scope) == {tombstoned, absent}
    finally:
        store.close()

    remaining = {row["transaction_id"] for row in _current_rows(db, "api")}
    assert tombstoned not in remaining and absent not in remaining
    assert remaining == seeded - {tombstoned, absent}


def test_a_proposal_about_a_retired_transaction_is_rejected(tmp_path: Path, capsys):
    """The packet still offers it, because a packet describes the past.

    Without this check the runtime would append an immutable decision about a
    transaction that is no longer there, and say nothing about it having gone.
    """
    db = tmp_path / "retire.sqlite"
    report = tmp_path / "review.html"
    source = tmp_path / "acceptance.csv"
    source.write_text(
        (FIXTURE_DIR / "acceptance.csv").read_text(encoding="utf-8"), encoding="utf-8"
    )
    _run_ingest(["ingest", "--source", "csv", str(source), "--db", str(db)])
    _run_analyze(db, report)
    packet_path = report.parent / "review-packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    subscription = next(
        item for item in packet["transactions"] if item["category"] == "Subscriptions"
    )
    target = subscription["transaction_id"]

    # A later complete snapshot no longer contains that row.
    store = Store(db)
    retiring = store.start_run("csv", "complete scan without it")
    # Under the CSV scope the ingest wrote: a run in the legacy scope would be
    # a complete scan of a different, empty dataset and would retire nothing.
    store.record_run_scope(retiring, None, sync_scope.csv_scope().key())
    remaining = {
        row["transaction_id"] for row in _current_rows(db, "csv") if row["transaction_id"] != target
    }
    assert store.retire_absent_snapshot(retiring, remaining) == 1
    store.finish_run(retiring, RUN_SUCCEEDED, len(remaining), complete_snapshot=True)
    store.commit()
    store.close()

    proposals = tmp_path / "proposals.json"
    proposals.write_text(json.dumps(_proposals_document(packet, target)), encoding="utf-8")
    out = tmp_path / "decisions.json"

    assert _run_decide(db, packet_path, proposals, out) == 1
    err = capsys.readouterr().err
    assert "retired_transaction_id" in err
    assert "re-run `analyze`" in err
    assert not out.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM decision_record").fetchone()[0] == 0


def test_a_proposal_about_a_surviving_transaction_still_succeeds(tmp_path: Path):
    """The retirement check must not reject transactions that are simply fine."""
    db = tmp_path / "survive.sqlite"
    report = tmp_path / "review.html"
    source = tmp_path / "acceptance.csv"
    source.write_text(
        (FIXTURE_DIR / "acceptance.csv").read_text(encoding="utf-8"), encoding="utf-8"
    )
    _run_ingest(["ingest", "--source", "csv", str(source), "--db", str(db)])
    _run_analyze(db, report)
    packet_path = report.parent / "review-packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    subscription = next(
        item for item in packet["transactions"] if item["category"] == "Subscriptions"
    )

    proposals = tmp_path / "proposals.json"
    proposals.write_text(
        json.dumps(_proposals_document(packet, subscription["transaction_id"])), encoding="utf-8"
    )
    out = tmp_path / "decisions.json"

    assert _run_decide(db, packet_path, proposals, out) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["appended_count"] == 1
