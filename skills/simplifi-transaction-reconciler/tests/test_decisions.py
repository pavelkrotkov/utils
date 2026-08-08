"""Coverage for the agent boundary: proposals in, append-only records out."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from simplifi_runtime import decisions
from simplifi_runtime.decisions import (
    MUTATION_ACTIONS,
    SUPPORTED_ACTIONS,
    VALIDATOR_VERSION,
    ProposalValidationError,
    build_decision_document,
    build_decision_records,
    validate_proposals,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
ALLOWED_CATEGORIES = {"Shopping", "Groceries"}

#: Override sentinel: drop the field instead of setting it.
REMOVE = object()


def _packet() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "review_packet.json").read_text(encoding="utf-8"))


def _document(**proposal_overrides: Any) -> dict[str, Any]:
    document = json.loads((FIXTURE_DIR / "proposals.json").read_text(encoding="utf-8"))
    for key, value in proposal_overrides.items():
        if value is REMOVE:
            document["proposals"][0].pop(key, None)
        else:
            document["proposals"][0][key] = value
    return document


def _codes(document: dict[str, Any], **kwargs: Any) -> list[str]:
    """Validate and return the rejection codes, most specific paths intact."""
    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposals(document, _packet(), allowed_categories=ALLOWED_CATEGORIES, **kwargs)
    return [error.code for error in excinfo.value.errors]


def test_fixture_document_satisfies_the_documented_contract():
    validated = validate_proposals(
        _document(), _packet(), allowed_categories=ALLOWED_CATEGORIES, latest_run_id=42
    )

    assert len(validated) == 1
    proposal = validated[0]
    assert proposal.transaction_id == "txn-fixture"
    assert proposal.decision == "accept"
    assert proposal.action == "record_category_proposal"
    assert proposal.category == "Shopping"
    assert proposal.policy_references == ("ADR-002", "ADR-004")
    assert len(proposal.proposal_hash) == 64


def test_unknown_transaction_ids_cannot_reach_a_decision():
    assert _codes(_document(transaction_id="not-in-packet")) == ["unknown_transaction_id"]


def test_malformed_decisions_are_rejected():
    assert _codes(_document(decision="looks-fine")) == ["malformed_decision"]
    assert _codes(_document(decision=REMOVE)) == ["malformed_decision"]


def test_mutation_actions_are_rejected_against_the_read_only_boundary():
    for action in MUTATION_ACTIONS:
        with pytest.raises(ProposalValidationError) as excinfo:
            validate_proposals(
                _document(action=action), _packet(), allowed_categories=ALLOWED_CATEGORIES
            )
        error = excinfo.value.errors[0]
        assert error.code == "unsupported_action"
        assert "read-only" in error.message

    assert not set(SUPPORTED_ACTIONS) & set(MUTATION_ACTIONS)


def test_unsupported_and_incoherent_actions_are_rejected():
    assert _codes(_document(action="teleport")) == ["unsupported_action"]
    assert _codes(_document(decision="escalate", action="dismiss_finding", category=REMOVE)) == [
        "unsupported_action_for_decision"
    ]


def test_categories_outside_the_dataset_taxonomy_are_rejected():
    assert _codes(_document(category="Invented Category")) == ["invalid_category"]
    assert _codes(_document(category=REMOVE)) == ["invalid_category"]
    assert _codes(_document(decision="reject", action="dismiss_finding", category="Shopping")) == [
        "unexpected_category"
    ]


def test_rationale_must_carry_evidence():
    assert _codes(_document(rationale=REMOVE)) == ["missing_rationale"]
    assert _codes(_document(rationale="   ")) == ["missing_rationale"]
    assert _codes(_document(rationale="fine")) == ["missing_rationale"]


def test_stale_run_and_dataset_references_are_rejected():
    stale_run = _document()
    stale_run["packet"]["run_id"] = 41
    assert _codes(stale_run) == ["stale_run_reference"]

    stale_dataset = _document()
    stale_dataset["packet"]["dataset_hash"] = "b" * 64
    assert _codes(stale_dataset) == ["stale_packet_reference"]

    superseded = _codes(_document(), latest_run_id=43)
    assert superseded == ["stale_run_reference"]


def test_smuggled_provider_instructions_are_rejected():
    document = _document(endpoint="/transactions/txn-fixture", method="PUT")
    document["callback_url"] = "https://example.invalid/write"

    codes = _codes(document)

    assert codes.count("unsupported_field") == 2


def test_duplicate_proposal_and_transaction_ids_are_rejected():
    document = _document()
    document["proposals"].append(copy.deepcopy(document["proposals"][0]))

    codes = _codes(document)

    assert codes == ["duplicate_proposal_id", "duplicate_transaction_id"]


def test_reviewer_identity_is_required():
    document = _document()
    document["reviewer"] = {"kind": "robot", "id": "  "}

    assert _codes(document) == ["malformed_reviewer", "malformed_reviewer"]

    missing = _document()
    del missing["reviewer"]
    assert _codes(missing) == ["malformed_reviewer"]


def test_confidence_must_be_a_unit_interval():
    assert _codes(_document(confidence=1.5)) == ["malformed_confidence"]
    assert _codes(_document(confidence="high")) == ["malformed_confidence"]


def test_unsupported_document_envelope_is_rejected():
    document = _document()
    document["document_type"] = "simplifi.transaction.review"
    document["schema_version"] = "99"

    assert _codes(document) == ["unsupported_document_type", "unsupported_schema_version"]

    empty = _document()
    empty["proposals"] = []
    assert _codes(empty) == ["malformed_proposals"]


def test_every_rejection_is_reported_together_and_nothing_is_returned():
    document = _document(
        transaction_id="not-in-packet",
        decision="maybe",
        rationale="no",
        category="Invented Category",
    )

    with pytest.raises(ProposalValidationError) as excinfo:
        validate_proposals(document, _packet(), allowed_categories=ALLOWED_CATEGORIES)

    codes = {error.code for error in excinfo.value.errors}
    assert codes == {
        "unknown_transaction_id",
        "malformed_decision",
        "missing_rationale",
        "invalid_category",
    }
    assert all(error.path.startswith("proposals[0]") for error in excinfo.value.errors)
    assert all("proposals[0]" in str(error) for error in excinfo.value.errors)


def test_records_carry_the_full_audit_trail():
    packet = _packet()
    document = _document()
    validated = validate_proposals(document, packet, allowed_categories=ALLOWED_CATEGORIES)

    records = build_decision_records(
        validated, packet, document["reviewer"], recorded_at="2026-08-16T09:00:00+00:00"
    )

    assert len(records) == 1
    record = records[0]
    assert record["decision_id"].startswith("decision-")
    assert record["run_id"] == 42
    assert record["transaction_id"] == "txn-fixture"
    assert record["proposal_id"] == "proposal-1"
    assert record["proposal_hash"] == validated[0].proposal_hash
    assert record["decision"] == "accept"
    assert record["action"] == "record_category_proposal"
    assert record["category"] == "Shopping"
    assert record["rationale"].startswith("Settled charge")
    assert record["recorded_at"] == "2026-08-16T09:00:00+00:00"
    assert record["validator_version"] == VALIDATOR_VERSION
    assert record["dataset_hash"] == packet["source"]["dataset_hash"]
    assert record["reviewer_kind"] == "agent"


def test_decision_ids_are_stable_and_follow_the_proposal_content():
    packet = _packet()
    document = _document()
    first = build_decision_records(
        validate_proposals(document, packet, allowed_categories=ALLOWED_CATEGORIES),
        packet,
        document["reviewer"],
        recorded_at="2026-08-16T09:00:00+00:00",
    )
    same = build_decision_records(
        validate_proposals(document, packet, allowed_categories=ALLOWED_CATEGORIES),
        packet,
        document["reviewer"],
        recorded_at="2026-08-17T10:30:00+00:00",
    )
    revised_document = _document(rationale="Revised after checking the merchant's cleared history.")
    revised = build_decision_records(
        validate_proposals(revised_document, packet, allowed_categories=ALLOWED_CATEGORIES),
        packet,
        revised_document["reviewer"],
        recorded_at="2026-08-16T09:00:00+00:00",
    )

    assert first[0]["decision_id"] == same[0]["decision_id"]
    assert revised[0]["decision_id"] != first[0]["decision_id"]


def test_decision_document_is_written_separately_from_the_packet(tmp_path: Path):
    packet = _packet()
    document = _document()
    validated = validate_proposals(document, packet, allowed_categories=ALLOWED_CATEGORIES)
    records = build_decision_records(
        validated, packet, document["reviewer"], recorded_at="2026-08-16T09:00:00+00:00"
    )

    out = tmp_path / "decisions.json"
    decisions.write_decisions(
        build_decision_document(packet, document["reviewer"], records, appended_count=1), out
    )
    written = json.loads(out.read_text(encoding="utf-8"))

    assert written["document_type"] == "simplifi.transaction.decisions"
    assert written["validator_version"] == VALIDATOR_VERSION
    assert written["packet"]["run_id"] == 42
    assert written["summary"] == {
        "decision_count": 1,
        "appended_count": 1,
        "already_recorded_count": 0,
    }
    assert written["records"] == records
    assert "transactions" not in written
