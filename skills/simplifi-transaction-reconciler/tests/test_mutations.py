"""Provider writes: authorization, preconditions, audit, and undo.

Every test here drives the real path — plan, precondition check, write, job
poll, audit, undo — against a fixture writer that never opens a socket.
Financial writes are not something to exercise against a real account in order
to find out whether the code works, and a fixture is also the only way to make
the interesting cases (a job that fails, a transaction someone else edited, a
process that dies mid-write) happen on demand.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from simplifi_runtime import mutations
from simplifi_runtime.store import RUN_SUCCEEDED, Store

AUTH = mutations.Authorization(
    authorized_by="pavel", note="approved the four grocery recategorizations"
)


class FixtureWriter(mutations.TransactionWriter):
    """A provider that records what it was asked to do.

    `job_sequence` is the run of statuses `/job-statuses/{id}` returns, so a
    test can make a write settle immediately, settle after a poll or two, fail,
    or never settle at all.
    """

    def __init__(self, documents, *, job_sequence=("success",), fail_write=None):
        self.documents = {doc["id"]: doc for doc in documents}
        self.job_sequence = list(job_sequence)
        self.fail_write = fail_write
        self.writes: list[dict] = []
        self.fetches: list[str] = []

    def fetch(self, transaction_id):
        self.fetches.append(transaction_id)
        return json.loads(json.dumps(self.documents[transaction_id]))

    def write(self, document):
        if self.fail_write:
            raise self.fail_write
        self.writes.append(json.loads(json.dumps(dict(document))))
        self.documents[document["id"]] = json.loads(json.dumps(dict(document)))
        return {"id": f"job-{len(self.writes)}", "status": "pending"}

    def job_status(self, job_id):
        status = self.job_sequence.pop(0) if self.job_sequence else "pending"
        return {"id": job_id, "status": status}


def _document(transaction_id="txn-1", category="Uncategorized", **extra):
    """The 18-field GET shape, trimmed to what the write path touches."""
    return {
        "id": transaction_id,
        "accountId": "account-9",
        "amount": -1240,
        "coa": {"id": "coa-3", "name": category, "type": "CATEGORY"},
        "dbVersion": 7,
        "payee": "SQ *AURORA BAKERY",
        "postedOn": "2026-06-01",
        "state": "CLEARED",
        **extra,
    }


def _row(transaction_id="txn-1", category="Uncategorized"):
    return {
        "transaction_id": transaction_id,
        "category": category,
        "source_hash": "hash-1",
        "payee_display": "Aurora Bakery",
        "posted_on": "2026-06-01",
    }


def _decision(transaction_id="txn-1", category="Groceries", run_id=1, **extra):
    return {
        "decision_id": f"decision-{transaction_id}",
        "run_id": run_id,
        "source": "api",
        "transaction_id": transaction_id,
        "category": category,
        "decision": "accept",
        "action": "record_category_proposal",
        "proposal_hash": "proposal-hash-1",
        **extra,
    }


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "mutate.sqlite")
    run_id = store.start_run("api", "fixture")
    store.finish_run(run_id, RUN_SUCCEEDED, 1)
    store.commit()
    yield store
    store.close()


def _plan(decisions=None, rows=None, **kwargs):
    return mutations.plan_category_writes(
        decisions if decisions is not None else [_decision()],
        rows if rows is not None else [_row()],
        latest_run_id=kwargs.pop("latest_run_id", 1),
        **kwargs,
    )


# --- capabilities and risk --------------------------------------------------


def test_every_capability_documents_its_risk_and_blast_radius():
    for name, cap in mutations.CAPABILITIES.items():
        assert cap.risk, f"{name} has no risk assessment"
        assert cap.blast_radius, f"{name} does not say how far a mistake reaches"
        assert cap.evidence, f"{name} does not say how its behavior is known"
        if not cap.available:
            assert cap.unavailable_because, f"{name} is refused without saying why"


def test_only_verified_capabilities_are_available():
    """An unverified endpoint is not something to try on a live account."""
    for cap in mutations.CAPABILITIES.values():
        if cap.available:
            assert cap.evidence.lower().startswith("verified")


@pytest.mark.parametrize("name", ["transaction_rule", "institution_refresh", "transaction_delete"])
def test_an_unavailable_capability_refuses_with_its_reason(name):
    with pytest.raises(mutations.MutationError, match="not available"):
        mutations.capability(name)


def test_an_unknown_capability_names_what_was_asked_for():
    with pytest.raises(mutations.MutationError, match="unknown mutation capability"):
        mutations.capability("teleport_transaction")


# --- authorization ----------------------------------------------------------


def test_an_authorization_must_name_a_person_and_a_reason():
    with pytest.raises(mutations.AuthorizationError):
        mutations.Authorization(authorized_by="", note="a perfectly good reason")
    with pytest.raises(mutations.AuthorizationError):
        mutations.Authorization(authorized_by="pavel", note="ok")


def test_applying_without_an_authorization_object_is_refused(store):
    plan, _ = _plan()
    with pytest.raises(mutations.AuthorizationError):
        mutations.apply_plan(store, plan, FixtureWriter([_document()]), authorization=True)


# --- planning ---------------------------------------------------------------


def test_only_accepted_category_decisions_become_mutations():
    decisions = [
        _decision("txn-1"),
        _decision("txn-2", **{"decision": "reject"}),
        _decision("txn-3", **{"action": "request_human_review"}),
    ]
    rows = [_row("txn-1"), _row("txn-2"), _row("txn-3")]

    plan, _ = _plan(decisions, rows)

    assert [item.transaction_id for item in plan] == ["txn-1"]


def test_a_decision_about_a_superseded_run_is_skipped():
    plan, skipped = _plan([_decision(run_id=1)], [_row()], latest_run_id=2)

    assert plan == []
    assert "reviewed run 1" in skipped[0]


def test_a_retired_transaction_is_skipped():
    plan, skipped = _plan(retired_transaction_ids={"txn-1"})

    assert plan == []
    assert "retired since the review" in skipped[0]


def test_a_transaction_that_is_no_longer_current_is_skipped():
    plan, skipped = _plan(rows=[])

    assert plan == []
    assert "not a current transaction" in skipped[0]


def test_a_transaction_already_holding_the_target_category_is_skipped():
    plan, skipped = _plan(rows=[_row(category="Groceries")])

    assert plan == []
    assert "already 'Groceries'" in skipped[0]


def test_an_already_applied_decision_is_not_written_twice():
    plan, skipped = _plan(already_applied={"decision-txn-1"})

    assert plan == []
    assert "already applied" in skipped[0]


# --- dry run ----------------------------------------------------------------


def test_the_dry_run_describes_the_request_that_would_be_made():
    plan, skipped = _plan()

    rendered = mutations.render_plan(plan, skipped)

    assert "PUT /transactions/txn-1" in rendered
    assert "from        : (uncategorized)" in rendered or "Uncategorized" in rendered
    assert "to          : Groceries" in rendered
    assert "Nothing has been sent" in rendered


def test_the_dry_run_lists_what_it_skipped_and_why():
    rendered = mutations.render_plan([], ["decision-txn-1: already applied by an earlier mutation"])

    assert "0 mutation(s) planned, 1 skipped." in rendered
    assert "already applied" in rendered


# --- writing ----------------------------------------------------------------


def test_a_successful_write_sends_the_whole_document_with_one_field_changed(store):
    plan, _ = _plan()
    writer = FixtureWriter([_document()])

    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)

    assert [result.outcome for result in results] == ["succeeded"]
    sent = writer.writes[0]
    # Every other field survives: a full-document PUT replaces the provider's
    # copy, so a document assembled from anything less reverts what it omits.
    assert sent["payee"] == "SQ *AURORA BAKERY"
    assert sent["dbVersion"] == 7
    assert sent["accountId"] == "account-9"
    assert sent["coa"]["name"] == "Groceries"
    assert sent["coa"]["id"] == "coa-3", "the category object keeps its other fields"


def test_the_audit_preserves_the_document_as_it_was_before_the_write(store):
    plan, _ = _plan()
    writer = FixtureWriter([_document()])

    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)

    attempt = store.mutation_attempt(results[0].attempt_id)
    before = json.loads(attempt["before_document"])
    after = json.loads(attempt["after_document"])
    assert before["coa"]["name"] == "Uncategorized"
    assert after["coa"]["name"] == "Groceries"
    assert attempt["authorized_by"] == "pavel"
    assert attempt["authorization_note"] == AUTH.note
    assert attempt["decision_id"] == "decision-txn-1"


def test_a_failed_job_is_recorded_as_failed_not_as_success(store):
    plan, _ = _plan()
    writer = FixtureWriter([_document()], job_sequence=["failed"])

    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)

    assert results[0].outcome == "failed"
    assert store.mutation_outcome(results[0].attempt_id)["outcome"] == "failed"


def test_a_transport_failure_is_recorded_as_unknown(store):
    """A write that left with no answer may have landed. That is not a failure."""
    plan, _ = _plan()
    writer = FixtureWriter([_document()], fail_write=OSError("connection reset"))

    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)

    assert results[0].outcome == "unknown"
    outcome = store.mutation_outcome(results[0].attempt_id)
    assert outcome["outcome"] == "unknown"
    assert outcome["error_class"] == "OSError"
    # And the attempt itself is on record, because it was written first.
    assert store.mutation_attempt(results[0].attempt_id) is not None


def test_an_unknown_outcome_is_not_retried_by_a_later_run(store):
    plan, _ = _plan()
    writer = FixtureWriter([_document()], fail_write=OSError("connection reset"))
    mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)
    store.commit()

    assert store.applied_decision_ids() == {"decision-txn-1"}


def test_a_failed_write_may_be_planned_again(store):
    """A rejected write changed nothing, so re-proposing it is not a double-apply."""
    plan, _ = _plan()
    mutations.apply_plan(
        store,
        plan,
        FixtureWriter([_document()], job_sequence=["failed"]),
        AUTH,
        sleep=lambda _: None,
    )
    store.commit()

    assert store.applied_decision_ids() == set()


def test_a_transaction_edited_since_planning_is_refused(store):
    """The plan is a statement about the past; the provider is the present."""
    plan, _ = _plan()
    writer = FixtureWriter([_document(category="Dining Out")])

    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)

    assert results[0].outcome == "failed"
    assert "at the provider now" in results[0].error_message
    assert writer.writes == [], "nothing may be sent once a precondition fails"
    assert store.mutation_attempt(results[0].attempt_id) is None


def test_a_transaction_already_holding_the_target_at_the_provider_is_refused(store):
    plan, _ = _plan()
    writer = FixtureWriter([_document(category="Groceries")])

    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)

    assert results[0].outcome == "failed"
    assert writer.writes == []


def test_the_audit_trail_cannot_be_edited_or_deleted(store):
    plan, _ = _plan()
    results = mutations.apply_plan(
        store, plan, FixtureWriter([_document()]), AUTH, sleep=lambda _: None
    )
    store.commit()

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute(
            "UPDATE mutation_attempt SET change_summary = 'nothing happened' WHERE attempt_id = ?",
            (results[0].attempt_id,),
        )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute("DELETE FROM mutation_outcome")


# --- undo -------------------------------------------------------------------


def test_undo_restores_the_preserved_document(store):
    plan, _ = _plan()
    writer = FixtureWriter([_document()], job_sequence=["success", "success"])
    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)
    store.commit()

    undone = mutations.undo(store, results[0].attempt_id, writer, AUTH, sleep=lambda _: None)

    assert undone.succeeded
    assert writer.documents["txn-1"]["coa"]["name"] == "Uncategorized"
    assert writer.documents["txn-1"]["payee"] == "SQ *AURORA BAKERY"


def test_undo_appends_rather_than_erasing(store):
    plan, _ = _plan()
    writer = FixtureWriter([_document()], job_sequence=["success", "success"])
    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)
    store.commit()
    original = results[0].attempt_id

    undone = mutations.undo(store, original, writer, AUTH, sleep=lambda _: None)

    assert store.mutation_attempt(original) is not None, "the original write still happened"
    assert store.mutation_attempt(undone.attempt_id)["undoes_attempt_id"] == original
    assert store.undo_of(original)["attempt_id"] == undone.attempt_id


def test_a_mutation_cannot_be_undone_twice(store):
    plan, _ = _plan()
    writer = FixtureWriter([_document()], job_sequence=["success"] * 4)
    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)
    store.commit()
    mutations.undo(store, results[0].attempt_id, writer, AUTH, sleep=lambda _: None)

    with pytest.raises(mutations.MutationError, match="already been undone"):
        mutations.undo(store, results[0].attempt_id, writer, AUTH, sleep=lambda _: None)


def test_an_unsettled_mutation_cannot_be_undone(store):
    """Undoing a write that may not have landed would write a guess."""
    plan, _ = _plan()
    writer = FixtureWriter([_document()], fail_write=OSError("connection reset"))
    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)
    store.commit()

    with pytest.raises(mutations.MutationError, match="only a settled successful write"):
        mutations.undo(
            store, results[0].attempt_id, FixtureWriter([_document()]), AUTH, sleep=lambda _: None
        )


def test_undo_refuses_to_overwrite_someone_elses_change(store):
    plan, _ = _plan()
    writer = FixtureWriter([_document()], job_sequence=["success"] * 4)
    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)
    store.commit()
    # Somebody recategorizes it in the app afterwards.
    writer.documents["txn-1"]["coa"]["name"] = "Dining Out"

    with pytest.raises(mutations.PreconditionError, match="Someone else changed it"):
        mutations.undo(store, results[0].attempt_id, writer, AUTH, sleep=lambda _: None)


def test_undo_requires_its_own_authorization(store):
    plan, _ = _plan()
    writer = FixtureWriter([_document()], job_sequence=["success"] * 4)
    results = mutations.apply_plan(store, plan, writer, AUTH, sleep=lambda _: None)
    store.commit()

    with pytest.raises(mutations.AuthorizationError):
        mutations.undo(
            store, results[0].attempt_id, writer, authorization=None, sleep=lambda _: None
        )


def test_undoing_an_unknown_attempt_says_so(store):
    with pytest.raises(mutations.MutationError, match="no mutation attempt"):
        mutations.undo(store, "not-a-real-attempt", FixtureWriter([]), AUTH, sleep=lambda _: None)


# --- what the command line says it is doing ---------------------------------


def test_a_dry_run_declares_no_provider_traffic():
    from simplifi_runtime import egress

    described = egress.mutation_declaration("mutate", applying=False).describe()

    assert "makes no network calls" in described
    assert "WRITES" not in described


def test_applying_declares_that_it_writes_not_that_it_reads():
    """Declaring a write as a read is false to the person most entitled to know."""
    from simplifi_runtime import egress

    described = egress.mutation_declaration("mutate", applying=True).describe()

    assert "WRITES to your Simplifi account" in described


# --- the command line, end to end -------------------------------------------


def _run(arguments):
    from simplifi_runtime.cli import build_parser

    args = build_parser().parse_args(arguments)
    return args.func(args)


def test_the_dry_run_command_sends_nothing_and_needs_no_writer(tmp_path, monkeypatch, capsys):
    """The default is a description. It must not need a provider to produce one."""
    from simplifi_runtime import cli

    db = tmp_path / "mutate.sqlite"
    store = Store(db)
    run_id = store.start_run("api", "fixture")
    store.finish_run(run_id, RUN_SUCCEEDED, 1)
    store.upsert_version(
        run_id,
        {
            "transaction_id": "txn-1",
            "posted_on": "2026-06-01",
            "amount_minor_units": -1240,
            "currency": "USD",
            "payee_raw": "SQ *AURORA BAKERY",
            "payee_normalized": "aurora bakery",
            "payee_canonical": "aurora_bakery",
            "payee_display": "Aurora Bakery",
            "norm_rules_applied": "",
            "account_name": "Checking",
            "account_id": "account-9",
            "currency_exponent": 2,
            "category": "Uncategorized",
            "inferred_category": None,
            "is_uncategorized": 1,
            "exclusion_flag": 0,
            "recurring_flag": 0,
            "kind": "spend",
            "poisons_statistics": 0,
            "semantics_reasons": "",
            "txn_state": "CLEARED",
            "transacted_on": None,
            "original_currency": None,
            "original_amount": None,
            "is_foreign_charge": 0,
            "match_state": None,
            "scheduled_model_id": None,
            "scheduled_due_on": None,
        },
    )
    store.append_decision_records(
        [
            {
                "decision_id": "decision-txn-1",
                "run_id": run_id,
                "source": "api",
                "analysis_date": "2026-06-15",
                "transaction_id": "txn-1",
                "proposal_id": "proposal-1",
                "proposal_hash": "hash-1",
                "dataset_hash": "dataset-1",
                "decision": "accept",
                "action": "record_category_proposal",
                "category": "Groceries",
                "rationale": "the merchant is a bakery and the amount is typical",
                "reviewer_kind": "human",
                "reviewer_id": "pavel",
                "recorded_at": "2026-06-15T00:00:00+00:00",
                "validator_version": "1.0.0",
            }
        ]
    )
    store.commit()
    store.close()

    def _refuse_writer(_args):
        raise AssertionError("a dry run must not construct a provider client")

    monkeypatch.setattr(cli, "_transaction_writer", _refuse_writer)

    assert _run(["mutate", "--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert "1 mutation(s) planned" in out
    assert "PUT /transactions/txn-1" in out
    assert "to          : Groceries" in out
    assert "Nothing has been sent" in out


def test_applying_without_an_authorization_refuses_at_the_command_line(tmp_path, capsys):
    db = tmp_path / "mutate.sqlite"
    store = Store(db)
    run_id = store.start_run("api", "fixture")
    store.finish_run(run_id, RUN_SUCCEEDED, 0)
    store.commit()
    store.close()

    assert _run(["mutate", "--db", str(db), "--apply"]) == 2
    assert "--authorized-by" in capsys.readouterr().err


def test_the_capability_register_is_printable_without_a_database(capsys):
    assert _run(["mutate", "--capabilities"]) == 0

    printed = capsys.readouterr().out
    assert "transaction_category  [available]" in printed
    assert "transaction_rule  [UNAVAILABLE]" in printed
    assert "refused" in printed
