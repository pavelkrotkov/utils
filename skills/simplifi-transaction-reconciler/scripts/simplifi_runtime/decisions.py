"""Validate agent judgment and turn it into append-only decision records.

The runtime writes ``review-packet.json``; an agent reads it and answers with a
structured ``proposals.json``.  This module is the gate between the two.  It
accepts a proposal only when it references the current run, names a transaction
the packet actually offered, requests a read-only follow-up, and carries a
rationale.  Everything else is rejected with a coded, actionable error.

Validation is atomic: a document with any rejected proposal records nothing.
An audit trail that silently kept half of a reviewer's judgment would be worse
than one that fails closed and asks for a corrected file.

No supported action changes provider state.  The vocabulary here describes what
a human should look at next, never a write, refresh, or undo request.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROPOSALS_TYPE = "simplifi.transaction.proposals"
PROPOSALS_VERSION = "1"
DECISIONS_TYPE = "simplifi.transaction.decisions"
DECISIONS_VERSION = "1"

#: Bumped whenever validation semantics change. Recorded with every decision so
#: a stored record stays interpretable after the rules move on.
VALIDATOR_VERSION = "1.0.0"

#: A rationale short enough to be a reflex ("ok", "fine") is not a rationale.
MIN_RATIONALE_LENGTH = 12

#: Verdicts a reviewer may record about a deterministic finding.
SUPPORTED_DECISIONS = ("accept", "defer", "escalate", "reject")

#: Follow-ups this read-only runtime can record. Every entry produces an
#: artifact or a human task; none of them touches a provider account.
SUPPORTED_ACTIONS = (
    "dismiss_finding",
    "none",
    "record_category_proposal",
    "request_human_review",
)

#: Named explicitly so a mutation-shaped proposal fails with the read-only
#: boundary as its reason instead of a generic "unknown action".
MUTATION_ACTIONS = (
    "apply_category",
    "approve_proposal",
    "create_rule",
    "delete_transaction",
    "merge_transactions",
    "refresh_institution",
    "set_category",
    "split_transaction",
    "undo",
    "update_transaction",
    "write_transaction",
)

#: An action is only valid for the verdict that justifies it.
_DECISION_ACTIONS = {
    "accept": {"record_category_proposal", "dismiss_finding", "none"},
    "defer": {"request_human_review", "none"},
    "escalate": {"request_human_review"},
    "reject": {"dismiss_finding", "none"},
}

_DOCUMENT_KEYS = ("document_type", "packet", "proposals", "reviewer", "schema_version")
_PACKET_REFERENCE_KEYS = (
    "analysis_date",
    "dataset_hash",
    "packet_type",
    "run_id",
    "schema_version",
)
_REVIEWER_KEYS = ("id", "kind")
_REVIEWER_KINDS = ("agent", "human")
_PROPOSAL_KEYS = (
    "action",
    "category",
    "confidence",
    "decision",
    "finding_reason_codes",
    "policy_references",
    "proposal_id",
    "rationale",
    "transaction_id",
)
_PROPOSAL_LIST_KEYS = ("finding_reason_codes", "policy_references")


@dataclass(frozen=True)
class ProposalError:
    """One rejected aspect of one proposal, addressed by JSON path."""

    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.code}: {self.message}"


class ProposalValidationError(ValueError):
    """Raised when a proposal document is malformed, stale, or unsafe."""

    def __init__(self, errors: Sequence[ProposalError]):
        self.errors = list(errors)
        super().__init__("; ".join(str(error) for error in self.errors))


@dataclass(frozen=True)
class ValidatedProposal:
    """A proposal that satisfied every rule, plus its content hash."""

    proposal_id: str
    transaction_id: str
    decision: str
    action: str
    rationale: str
    category: str | None = None
    confidence: float | None = None
    finding_reason_codes: tuple[str, ...] = field(default_factory=tuple)
    policy_references: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        """Return the normalized proposal that the content hash covers."""
        return {
            "action": self.action,
            "category": self.category,
            "confidence": self.confidence,
            "decision": self.decision,
            "finding_reason_codes": list(self.finding_reason_codes),
            "policy_references": list(self.policy_references),
            "proposal_id": self.proposal_id,
            "rationale": self.rationale,
            "transaction_id": self.transaction_id,
        }

    @property
    def proposal_hash(self) -> str:
        return hashlib.sha256(_canonical(self.as_dict()).encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: Everything that makes one judgment distinct from another. `recorded_at` is
#: deliberately absent, so re-recording an unchanged review is a no-op; the
#: reviewer and the reviewed packet are present, so two people reaching the
#: same conclusion, or one person reviewing a differently scoped packet, are
#: two records rather than one silently dropped.
_IDENTITY_FIELDS = (
    "analysis_date",
    "dataset_hash",
    "proposal_hash",
    "proposal_id",
    "reviewer_id",
    "reviewer_kind",
    "run_id",
    "transaction_id",
)


def decision_identifier(record: Mapping[str, Any]) -> str:
    """Derive a stable ID from everything that distinguishes one judgment."""
    seed = _canonical({field: record[field] for field in _IDENTITY_FIELDS})
    return "decision-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def packet_reference(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Return the identity a proposal document must echo back."""
    run = _mapping(packet.get("run"))
    source = _mapping(packet.get("source"))
    return {
        "analysis_date": str(run.get("analysis_date", "")),
        "dataset_hash": str(source.get("dataset_hash", "")),
        "packet_type": packet.get("packet_type"),
        "run_id": run.get("run_id"),
        "schema_version": packet.get("schema_version"),
    }


def _unknown_fields(value: Mapping[str, Any], allowed: Sequence[str]) -> list[str]:
    return sorted(str(key) for key in value if str(key) not in allowed)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _validate_packet_reference(
    reference: Any, packet: Mapping[str, Any], latest_run_id: int | None
) -> list[ProposalError]:
    expected = packet_reference(packet)
    if not isinstance(reference, Mapping):
        return [
            ProposalError(
                "packet",
                "malformed_packet_reference",
                f"packet must be an object echoing the review packet: {_canonical(expected)}",
            )
        ]

    errors = []
    unknown = _unknown_fields(reference, _PACKET_REFERENCE_KEYS)
    if unknown:
        errors.append(
            ProposalError(
                "packet",
                "unsupported_field",
                f"unsupported field(s) {unknown}; supported: {list(_PACKET_REFERENCE_KEYS)}",
            )
        )
    missing = [key for key in _PACKET_REFERENCE_KEYS if key not in reference]
    if missing:
        errors.append(
            ProposalError(
                "packet",
                "malformed_packet_reference",
                f"missing field(s) {missing}; copy them from review-packet.json",
            )
        )
    for key in _PACKET_REFERENCE_KEYS:
        if key not in reference or reference[key] == expected[key]:
            continue
        code = "stale_run_reference" if key == "run_id" else "stale_packet_reference"
        errors.append(
            ProposalError(
                f"packet.{key}",
                code,
                f"expected {expected[key]!r} from the supplied review packet, "
                f"found {reference[key]!r}; re-read the current packet and redo the review",
            )
        )
    if latest_run_id is not None and expected["run_id"] != latest_run_id:
        errors.append(
            ProposalError(
                "packet.run_id",
                "stale_run_reference",
                f"review packet run {expected['run_id']!r} has been superseded by run "
                f"{latest_run_id}; re-run `analyze` and review the new packet",
            )
        )
    return errors


def _validate_reviewer(reviewer: Any) -> list[ProposalError]:
    if not isinstance(reviewer, Mapping):
        return [
            ProposalError(
                "reviewer",
                "malformed_reviewer",
                "reviewer must be an object with 'kind' and 'id'",
            )
        ]
    errors = []
    unknown = _unknown_fields(reviewer, _REVIEWER_KEYS)
    if unknown:
        errors.append(
            ProposalError(
                "reviewer",
                "unsupported_field",
                f"unsupported field(s) {unknown}; supported: {list(_REVIEWER_KEYS)}",
            )
        )
    if reviewer.get("kind") not in _REVIEWER_KINDS:
        errors.append(
            ProposalError(
                "reviewer.kind",
                "malformed_reviewer",
                f"kind must be one of {list(_REVIEWER_KINDS)}, found {reviewer.get('kind')!r}",
            )
        )
    if not _text(reviewer.get("id")):
        errors.append(
            ProposalError(
                "reviewer.id",
                "malformed_reviewer",
                "id must be a non-empty string identifying the reviewer or model",
            )
        )
    return errors


def _validate_action(
    raw: Mapping[str, Any], path: str, decision: str | None
) -> list[ProposalError]:
    action = _text(raw.get("action"))
    if action in MUTATION_ACTIONS:
        return [
            ProposalError(
                f"{path}.action",
                "unsupported_action",
                f"{action!r} would change provider state; this runtime is read-only and "
                f"records only {list(SUPPORTED_ACTIONS)}",
            )
        ]
    if action not in SUPPORTED_ACTIONS:
        return [
            ProposalError(
                f"{path}.action",
                "unsupported_action",
                f"action must be one of {list(SUPPORTED_ACTIONS)}, found {raw.get('action')!r}",
            )
        ]
    if decision is not None and action not in _DECISION_ACTIONS[decision]:
        return [
            ProposalError(
                f"{path}.action",
                "unsupported_action_for_decision",
                f"decision {decision!r} supports {sorted(_DECISION_ACTIONS[decision])}, "
                f"found {action!r}",
            )
        ]
    return []


def _validate_category(
    raw: Mapping[str, Any], path: str, action: str, allowed_categories: set[str]
) -> list[ProposalError]:
    category = _text(raw.get("category"))
    if action != "record_category_proposal":
        if category:
            return [
                ProposalError(
                    f"{path}.category",
                    "unexpected_category",
                    f"category is only recorded for 'record_category_proposal', not {action!r}",
                )
            ]
        return []
    if not category:
        return [
            ProposalError(
                f"{path}.category",
                "invalid_category",
                "a category proposal must name a category already used by this dataset",
            )
        ]
    if category not in allowed_categories:
        return [
            ProposalError(
                f"{path}.category",
                "invalid_category",
                f"{category!r} is not a known category for this dataset; "
                f"{len(allowed_categories)} known categories are available, and this "
                "runtime cannot create one",
            )
        ]
    return []


def _validate_rationale(raw: Mapping[str, Any], path: str) -> list[ProposalError]:
    rationale = _text(raw.get("rationale"))
    if not rationale:
        return [
            ProposalError(
                f"{path}.rationale",
                "missing_rationale",
                "every proposal must state the evidence-based reason for its decision",
            )
        ]
    if len(rationale) < MIN_RATIONALE_LENGTH:
        return [
            ProposalError(
                f"{path}.rationale",
                "missing_rationale",
                f"rationale must be at least {MIN_RATIONALE_LENGTH} characters of "
                f"evidence, found {len(rationale)}",
            )
        ]
    return []


def _validate_confidence(raw: Mapping[str, Any], path: str) -> list[ProposalError]:
    confidence = raw.get("confidence")
    if confidence is None:
        return []
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return [
            ProposalError(
                f"{path}.confidence",
                "malformed_confidence",
                f"confidence must be a number between 0 and 1, found {confidence!r}",
            )
        ]
    if not 0.0 <= float(confidence) <= 1.0:
        return [
            ProposalError(
                f"{path}.confidence",
                "malformed_confidence",
                f"confidence must be between 0 and 1, found {confidence!r}",
            )
        ]
    return []


def _validate_string_list(raw: Mapping[str, Any], path: str, key: str) -> list[ProposalError]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not _text(item) for item in value):
        return [
            ProposalError(
                f"{path}.{key}",
                "malformed_proposal",
                f"{key} must be an array of non-empty strings, found {value!r}",
            )
        ]
    return []


def _validate_proposal(
    raw: Any,
    path: str,
    *,
    known_transaction_ids: set[str],
    allowed_categories: set[str],
    seen_proposal_ids: set[str],
    seen_transaction_ids: set[str],
) -> tuple[ValidatedProposal | None, list[ProposalError]]:
    if not isinstance(raw, Mapping):
        return None, [ProposalError(path, "malformed_proposal", "proposal must be an object")]

    errors: list[ProposalError] = []
    unknown = _unknown_fields(raw, _PROPOSAL_KEYS)
    if unknown:
        errors.append(
            ProposalError(
                path,
                "unsupported_field",
                f"unsupported field(s) {unknown}; a proposal records only {list(_PROPOSAL_KEYS)}",
            )
        )

    proposal_id = _text(raw.get("proposal_id"))
    if not proposal_id:
        errors.append(
            ProposalError(
                f"{path}.proposal_id",
                "malformed_proposal_id",
                "proposal_id must be a non-empty string that is unique in this document",
            )
        )
    elif proposal_id in seen_proposal_ids:
        errors.append(
            ProposalError(
                f"{path}.proposal_id",
                "duplicate_proposal_id",
                f"{proposal_id!r} is already used by an earlier proposal",
            )
        )
    seen_proposal_ids.add(proposal_id)

    transaction_id = _text(raw.get("transaction_id"))
    if not transaction_id:
        errors.append(
            ProposalError(
                f"{path}.transaction_id",
                "unknown_transaction_id",
                "transaction_id must name a transaction from the review packet",
            )
        )
    elif transaction_id not in known_transaction_ids:
        errors.append(
            ProposalError(
                f"{path}.transaction_id",
                "unknown_transaction_id",
                f"{transaction_id!r} is not in the review packet's transaction_ids; "
                "a proposal cannot reach a transaction the packet did not offer",
            )
        )
    elif transaction_id in seen_transaction_ids:
        errors.append(
            ProposalError(
                f"{path}.transaction_id",
                "duplicate_transaction_id",
                f"{transaction_id!r} already has a decision in this document; "
                "record one decision per transaction",
            )
        )
    seen_transaction_ids.add(transaction_id)

    decision = _text(raw.get("decision"))
    if decision not in SUPPORTED_DECISIONS:
        errors.append(
            ProposalError(
                f"{path}.decision",
                "malformed_decision",
                f"decision must be one of {list(SUPPORTED_DECISIONS)}, "
                f"found {raw.get('decision')!r}",
            )
        )
        decision = ""

    action_errors = _validate_action(raw, path, decision or None)
    errors.extend(action_errors)
    action = _text(raw.get("action"))
    if not action_errors:
        errors.extend(_validate_category(raw, path, action, allowed_categories))
    errors.extend(_validate_rationale(raw, path))
    errors.extend(_validate_confidence(raw, path))
    for key in _PROPOSAL_LIST_KEYS:
        errors.extend(_validate_string_list(raw, path, key))

    if errors:
        return None, errors
    confidence = raw.get("confidence")
    return (
        ValidatedProposal(
            proposal_id=proposal_id,
            transaction_id=transaction_id,
            decision=decision,
            action=action,
            rationale=_text(raw.get("rationale")),
            category=_text(raw.get("category")) or None,
            confidence=None if confidence is None else round(float(confidence), 3),
            finding_reason_codes=tuple(
                sorted(_text(item) for item in (raw.get("finding_reason_codes") or []))
            ),
            policy_references=tuple(
                sorted(_text(item) for item in (raw.get("policy_references") or []))
            ),
        ),
        [],
    )


def validate_proposals(
    document: Any,
    packet: Mapping[str, Any],
    *,
    allowed_categories: Iterable[str],
    latest_run_id: int | None = None,
) -> list[ValidatedProposal]:
    """Validate a proposal document against one review packet.

    ``allowed_categories`` is the closed set of category labels the dataset
    already uses; the runtime cannot create a category, so a proposal may not
    name one. ``latest_run_id`` rejects a review of a superseded run.

    Raises :class:`ProposalValidationError` carrying every rejection reason.
    """
    if not isinstance(document, Mapping):
        raise ProposalValidationError(
            [ProposalError("document", "malformed_document", "proposals must be a JSON object")]
        )

    errors: list[ProposalError] = []
    unknown = _unknown_fields(document, _DOCUMENT_KEYS)
    if unknown:
        errors.append(
            ProposalError(
                "document",
                "unsupported_field",
                f"unsupported top-level field(s) {unknown}; supported: {list(_DOCUMENT_KEYS)}",
            )
        )
    if document.get("document_type") != PROPOSALS_TYPE:
        errors.append(
            ProposalError(
                "document.document_type",
                "unsupported_document_type",
                f"expected {PROPOSALS_TYPE!r}, found {document.get('document_type')!r}",
            )
        )
    if document.get("schema_version") != PROPOSALS_VERSION:
        errors.append(
            ProposalError(
                "document.schema_version",
                "unsupported_schema_version",
                f"expected {PROPOSALS_VERSION!r}, found {document.get('schema_version')!r}",
            )
        )
    errors.extend(_validate_packet_reference(document.get("packet"), packet, latest_run_id))
    errors.extend(_validate_reviewer(document.get("reviewer")))

    validated: list[ValidatedProposal] = []
    proposals = document.get("proposals")
    if not isinstance(proposals, list) or not proposals:
        errors.append(
            ProposalError(
                "proposals",
                "malformed_proposals",
                "proposals must be a non-empty array of proposal objects",
            )
        )
    else:
        known_transaction_ids = {
            str(item) for item in (packet.get("transaction_ids") or []) if str(item)
        }
        allowed = {_text(category) for category in allowed_categories if _text(category)}
        seen_proposal_ids: set[str] = set()
        seen_transaction_ids: set[str] = set()
        for index, raw in enumerate(proposals):
            proposal, item_errors = _validate_proposal(
                raw,
                f"proposals[{index}]",
                known_transaction_ids=known_transaction_ids,
                allowed_categories=allowed,
                seen_proposal_ids=seen_proposal_ids,
                seen_transaction_ids=seen_transaction_ids,
            )
            errors.extend(item_errors)
            if proposal is not None:
                validated.append(proposal)

    if errors:
        raise ProposalValidationError(errors)
    return validated


def build_decision_records(
    proposals: Sequence[ValidatedProposal],
    packet: Mapping[str, Any],
    reviewer: Mapping[str, Any],
    *,
    recorded_at: str | None = None,
) -> list[dict[str, Any]]:
    """Build the append-only records for a validated proposal document."""
    run = packet["run"]
    reference = packet_reference(packet)
    stamp = recorded_at or _now()
    records = []
    for proposal in proposals:
        record = {
            "run_id": int(reference["run_id"]),
            "source": str(run.get("source", "")),
            "analysis_date": str(reference["analysis_date"]),
            "transaction_id": proposal.transaction_id,
            "proposal_id": proposal.proposal_id,
            "proposal_hash": proposal.proposal_hash,
            "dataset_hash": str(reference["dataset_hash"]),
            "decision": proposal.decision,
            "action": proposal.action,
            "category": proposal.category,
            "rationale": proposal.rationale,
            "reviewer_kind": str(reviewer["kind"]),
            "reviewer_id": _text(reviewer["id"]),
            "recorded_at": stamp,
            "validator_version": VALIDATOR_VERSION,
        }
        records.append({"decision_id": decision_identifier(record), **record})
    records.sort(key=lambda record: (record["transaction_id"], record["proposal_id"]))
    return records


def build_decision_document(
    packet: Mapping[str, Any],
    reviewer: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    appended_count: int,
) -> dict[str, Any]:
    """Assemble the validated output written beside — never over — the packet."""
    return {
        "document_type": DECISIONS_TYPE,
        "schema_version": DECISIONS_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "packet": packet_reference(packet),
        "reviewer": {"kind": str(reviewer["kind"]), "id": _text(reviewer["id"])},
        "summary": {
            "decision_count": len(records),
            "appended_count": int(appended_count),
            "already_recorded_count": len(records) - int(appended_count),
        },
        "records": [dict(record) for record in records],
    }


def stage_decisions(document: Mapping[str, Any], path: Path) -> Path:
    """Write the document to a sibling temporary file and return its path.

    Publishing is a separate rename, so an unwritable destination fails before
    the database commit instead of after it. A caller that cannot produce the
    artifact must not leave immutable records behind that it cannot retract.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def publish_decisions(staged: Path, path: Path) -> None:
    """Move a staged document into place, replacing any previous version."""
    os.replace(staged, path)


def write_decisions(document: Mapping[str, Any], path: Path) -> None:
    """Write a canonical, newline-terminated decision document."""
    publish_decisions(stage_decisions(document, path), Path(path))
