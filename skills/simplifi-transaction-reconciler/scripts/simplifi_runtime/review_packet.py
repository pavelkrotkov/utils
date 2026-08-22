"""Versioned, deterministic artifact for agent-facing transaction review.

The packet is deliberately narrower than the SQLite row and source response.
It carries enough normalized evidence for judgment while excluding raw
descriptors, account IDs, source paths, and credentials.  The packet is a
terminal read-only artifact: it contains findings and proposals, never write
instructions or provider mutations.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import artifacts
from .evidence import UNKNOWN_ACCOUNT, UNKNOWN_MERCHANT, evidence_from_row
from .money import Money
from .semantics import SOURCE_CAPABILITIES, assess_eligibility
from .store import ALGORITHM_VERSION, RULESET_VERSION

PACKET_TYPE = "simplifi.transaction.review"
PACKET_VERSION = "1"

POLICY_REFERENCES = {
    "ADR-001": "references/adr/001-source-strategy.md",
    "ADR-002": "references/adr/002-merchant-identity-and-normalization.md",
    "ADR-003": "references/adr/003-accounting-semantics-and-projections.md",
    "ADR-004": "references/adr/004-deterministic-first-escalation.md",
    "ADR-006": "references/adr/006-provenance-and-incremental-storage.md",
}

_FINDING_POLICIES = {
    "amount_outlier": ("ADR-004",),
    "duplicate": ("ADR-004",),
    "new_merchant": ("ADR-002", "ADR-004"),
    "refund_without_original": ("ADR-003", "ADR-004"),
    "subscription_creep": ("ADR-003", "ADR-004"),
}

_FORBIDDEN_KEYS = {
    "access_token",
    "account_id",
    "account_ids",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "payee_raw",
    "raw_descriptor",
    "secret",
    "session_token",
    "source_path",
    "token",
}

_EXAMPLE_ALLOWED_KEYS = {
    "evidence",
    "human_decision",
    "id",
    "lesson",
    "policy_references",
    "proposal_or_escalation",
    "reusable_lesson",
    "situation",
    "title",
}
_EXAMPLE_FORBIDDEN_KEYS = _FORBIDDEN_KEYS | {
    "account",
    "account_name",
    "account_names",
    "source_hash",
    "transaction_id",
    "transaction_ids",
}
_MONEY_EVIDENCE_FIELDS = {"amount", "annual_impact", "median", "now", "previous_typical"}


class PacketValidationError(ValueError):
    """Raised when a packet does not satisfy the review-packet contract."""


def _json_safe(value: Any) -> Any:
    """Return a JSON-compatible value for deterministic evidence fields."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _assert_no_forbidden_keys(
    value: Any,
    path: str = "packet",
    forbidden_keys: set[str] | None = None,
) -> None:
    forbidden_keys = forbidden_keys or _FORBIDDEN_KEYS
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in forbidden_keys:
                raise PacketValidationError(f"{path} contains forbidden field {key!r}")
            _assert_no_forbidden_keys(item, f"{path}.{key}", forbidden_keys)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_forbidden_keys(item, f"{path}[{index}]", forbidden_keys)


def _string(value: Any) -> str:
    return str(value or "")


def _reason_codes(row: Mapping[str, Any]) -> list[str]:
    codes = {
        code.strip()
        for code in _string(row.get("eligibility_reason_codes")).split(",")
        if code.strip()
    }
    kind = _string(row.get("kind"))
    if kind:
        codes.add(f"accounting_kind:{kind}")
    if row.get("is_uncategorized"):
        codes.add("uncategorized")
    return sorted(codes)


def _eligible(row: Mapping[str, Any]) -> bool:
    return bool(row.get("review_eligible", assess_eligibility(dict(row)).eligible))


def _state(state: str) -> str:
    """`unknown` in lowercase, everything the source told us in its own case.

    The placeholder is deliberately distinguishable from a real provider state:
    a reader who sees `unknown` should not have to check whether Simplifi has a
    state by that name.
    """
    return "unknown" if state == "UNKNOWN" else state


def _merchant(row: Mapping[str, Any]) -> dict[str, str]:
    merchant = evidence_from_row(row).merchant
    return {
        "canonical": merchant.canonical,
        "display": merchant.safe_display() or UNKNOWN_MERCHANT,
        "normalized": merchant.normalized,
    }


def _transaction(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map a stored row to the packet's deliberately small transaction shape.

    Every field here comes from `evidence`, so the packet, the report and the
    model payload describe the same transaction the same way. This function
    previously re-derived the merchant display, the safe account name and the
    currency exponent itself, and the report derived all three differently.
    """
    evidence = evidence_from_row(row)
    return {
        "transaction_id": evidence.transaction_id,
        "posted_on": evidence.posted_on,
        "transacted_on": _json_safe(evidence.transacted_on),
        # `AccountRef.display` is the "unknown account" placeholder when the
        # source could not name it, and is never the provider's ID.
        "account_name": evidence.account.display or UNKNOWN_ACCOUNT,
        "merchant": _merchant(row),
        "amount": {
            "minor_units": evidence.money.minor_units,
            "currency": evidence.money.currency or "unknown",
            "currency_exponent": evidence.money.exponent,
        },
        "category": evidence.category or None,
        "inferred_category": evidence.inferred_category,
        "kind": evidence.kind,
        "transaction_state": _state(evidence.transaction_state),
        "match_state": evidence.match_state,
        "flags": {
            "uncategorized": evidence.uncategorized,
            "recurring": evidence.recurring,
            "split": evidence.split,
            "reviewed": evidence.reviewed,
            "foreign_charge": evidence.foreign_charge,
            "projected": evidence.projected,
        },
        "reason_codes": _reason_codes(row),
        "provenance": {
            "transaction_version_id": evidence.provenance.transaction_version_id,
            "run_id": evidence.provenance.run_id,
            "source_hash": evidence.provenance.source_hash,
            "algorithm_version": evidence.provenance.algorithm_version or ALGORITHM_VERSION,
            "ruleset_version": evidence.provenance.ruleset_version or RULESET_VERSION,
        },
    }


def dataset_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    identity = [
        {
            "transaction_id": _string(row.get("transaction_id")),
            "source_hash": _string(row.get("source_hash")),
        }
        for row in rows
    ]
    identity.sort(key=lambda item: item["transaction_id"])
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_evidence(evidence: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep rule facts while retaining monetary units and safe identity fields."""
    money = evidence_from_row(row).money
    result: dict[str, Any] = {}
    for key, value in sorted(evidence.items()):
        name = str(key)
        lower_name = name.lower()
        if lower_name in {"account", "merchant", "payee", "payee_raw"}:
            continue
        if lower_name in _MONEY_EVIDENCE_FIELDS:
            continue
        if lower_name.endswith("_minor_units"):
            base_name = lower_name.removesuffix("_minor_units")
            if base_name in _MONEY_EVIDENCE_FIELDS:
                fact = Money(int(value), money.currency)
                result[base_name] = {
                    "minor_units": fact.minor_units,
                    "currency": fact.currency,
                    "currency_exponent": fact.exponent,
                }
                continue
        result[name] = _json_safe(value)
    return result


def _finding_policies(reason_codes: list[str]) -> list[str]:
    refs: set[str] = {"ADR-004"}
    for reason in reason_codes:
        refs.update(_FINDING_POLICIES.get(reason, ()))
    return sorted(refs)


def _prioritized_findings(prioritized: list[Any]) -> list[dict[str, Any]]:
    findings = []
    for item in prioritized:
        row = item.row
        reason_codes = sorted({str(signal.name) for signal in item.signals})
        findings.append(
            {
                "transaction_id": _string(row.get("transaction_id")),
                "transaction_ids": [_string(row.get("transaction_id"))],
                "scope": "transaction",
                "priority": round(float(item.total_score), 2),
                "confidence": None,
                "confidence_basis": (
                    "Deterministic evidence is reported; no probabilistic confidence is assigned."
                ),
                "reason_codes": reason_codes,
                "evidence": [
                    {
                        "code": str(signal.name),
                        "score": round(float(signal.score), 2),
                        "facts": _safe_evidence(signal.evidence, row),
                    }
                    for signal in sorted(item.signals, key=lambda signal: signal.name)
                ],
                "policy_references": _finding_policies(reason_codes),
            }
        )
    return findings


def series_annual_impact(finding: Any, rows: Sequence[Mapping[str, Any]]) -> Money:
    """A recurring finding's yearly cost, in the currency of its own series.

    `annual_impact` reaches here as a float in major units — the one figure in
    the packet whose currency a reader could not determine, in the section that
    exists to say what a subscription costs per year. The series' own rows
    supply it, and both the packet and the report render through this, so the
    two artifacts cannot state the figure differently.
    """
    by_id = {_string(row.get("transaction_id")): row for row in rows}
    member = next(
        (by_id[str(txid)] for txid in sorted(finding.transaction_ids) if str(txid) in by_id),
        None,
    )
    currency = (_string(member.get("currency")) if member else "") or "USD"
    exponent = Money(0, currency).exponent
    return Money(round(float(finding.annual_impact) * (10**exponent)), currency)


def _subscription_findings(
    subscription_findings: list[Any], rows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    findings = []
    for finding in subscription_findings:
        reason = f"subscription:{finding.kind}"
        impact = series_annual_impact(finding, rows)
        findings.append(
            {
                "transaction_id": None,
                "transaction_ids": sorted(str(txid) for txid in finding.transaction_ids),
                "scope": "merchant_series",
                "priority": None,
                "confidence": None,
                "confidence_basis": (
                    "Deterministic recurring-series evidence; no probabilistic confidence is assigned."
                ),
                "reason_codes": [reason],
                "evidence": {
                    "merchant": _string(finding.merchant),
                    "detail": _string(finding.detail),
                    "annual_impact": {
                        "minor_units": impact.minor_units,
                        "currency": impact.currency,
                        "currency_exponent": impact.exponent,
                    },
                },
                "policy_references": ["ADR-003", "ADR-004"],
            }
        )
    return findings


def _category_proposals(
    proposals: Sequence[tuple[Mapping[str, Any], Any]],
) -> list[dict[str, Any]]:
    out = []
    for row, proposal in proposals:
        out.append(
            {
                "transaction_id": _string(row.get("transaction_id")),
                "category": proposal.category if proposal else None,
                "confidence": round(float(proposal.confidence), 3) if proposal else None,
                "evidence": proposal.evidence if proposal else {},
                "reason_codes": (
                    ["uncategorized", "merchant_memory"]
                    if proposal
                    else ["uncategorized", "deterministic_evidence_insufficient"]
                ),
                "policy_references": ["ADR-002", "ADR-003", "ADR-004"],
            }
        )
    return out


def build_packet(
    *,
    run_id: int,
    source: str,
    analysis_date: date | str,
    rows: list[dict[str, Any]],
    prioritized: list[Any],
    proposals: list[tuple[dict[str, Any], Any]],
    subscription_findings: list[Any] | None = None,
    stale_account_count: int = 0,
    limitations: list[str] | None = None,
    examples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a stable packet from deterministic analysis outputs.

    ``examples`` is intentionally caller-supplied. The CLI supplies only the
    explicitly promoted, sanitized judgment examples; transaction history is
    never promoted into this field.
    """
    if isinstance(analysis_date, date):
        analysis_date = analysis_date.isoformat()
    else:
        analysis_date = str(analysis_date)

    eligible_rows = sorted(
        (row for row in rows if _eligible(row)),
        key=lambda row: _string(row.get("transaction_id")),
    )
    excluded_rows = sorted(
        (row for row in rows if not _eligible(row)),
        key=lambda row: _string(row.get("transaction_id")),
    )
    capabilities = SOURCE_CAPABILITIES.get(source)
    if capabilities is None:
        raise PacketValidationError(f"unsupported source {source!r}")

    findings = _prioritized_findings(prioritized)
    findings.extend(_subscription_findings(list(subscription_findings or []), rows))
    findings.sort(
        key=lambda finding: (
            finding["transaction_id"] is None,
            finding["transaction_id"] or "",
            finding["reason_codes"],
        )
    )
    packet = {
        "packet_type": PACKET_TYPE,
        "schema_version": PACKET_VERSION,
        "run": {
            "run_id": int(run_id),
            "source": source,
            "analysis_date": analysis_date,
            "algorithm_version": ALGORITHM_VERSION,
            "ruleset_version": RULESET_VERSION,
        },
        "source": {
            "kind": source,
            "dataset_hash": dataset_hash(rows),
            "capabilities": {
                "stable_transaction_ids": capabilities.stable_transaction_id,
                "settlement_state": capabilities.settlement_state,
                "report_exclusion": capabilities.report_exclusion,
            },
        },
        "summary": {
            "transaction_count": len(rows),
            "eligible_transaction_count": len(eligible_rows),
            "excluded_transaction_count": len(excluded_rows),
            "finding_count": len(findings),
            "category_proposal_count": len(proposals),
            "unresolved_category_count": sum(1 for _, proposal in proposals if proposal is None),
            "stale_account_count": int(stale_account_count),
        },
        "transaction_ids": [_string(row.get("transaction_id")) for row in eligible_rows],
        "transactions": [_transaction(row) for row in eligible_rows],
        "excluded_transactions": [
            {
                "transaction_id": _string(row.get("transaction_id")),
                "reason_codes": _reason_codes(row),
            }
            for row in excluded_rows
        ],
        "findings": findings,
        "category_proposals": _category_proposals(proposals),
        "limitations": sorted(str(limitation) for limitation in (limitations or [])),
        "policy_references": [
            {"id": policy_id, "path": POLICY_REFERENCES[policy_id]}
            for policy_id in sorted(POLICY_REFERENCES)
        ],
        "examples": list(examples or []),
    }
    validate_packet(packet)
    return packet


def _require_mapping(packet: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = packet.get(key)
    if not isinstance(value, Mapping):
        raise PacketValidationError(f"{key} must be an object")
    return value


def _validate_examples(examples: list[Any]) -> None:
    for index, example in enumerate(examples):
        if not isinstance(example, Mapping):
            raise PacketValidationError(f"examples[{index}] must be an object")
        unknown = set(str(key) for key in example) - _EXAMPLE_ALLOWED_KEYS
        if unknown:
            raise PacketValidationError(
                f"examples[{index}] contains unsupported fields: {sorted(unknown)}"
            )
        _assert_no_forbidden_keys(example, f"examples[{index}]", _EXAMPLE_FORBIDDEN_KEYS)


def validate_packet(packet: Mapping[str, Any]) -> None:
    """Validate the stable packet contract before it crosses the file boundary."""
    if not isinstance(packet, Mapping):
        raise PacketValidationError("packet must be an object")
    if packet.get("packet_type") != PACKET_TYPE:
        raise PacketValidationError("packet_type is not supported")
    if packet.get("schema_version") != PACKET_VERSION:
        raise PacketValidationError("schema_version is not supported")

    run = _require_mapping(packet, "run")
    for key in ("run_id", "source", "analysis_date", "algorithm_version", "ruleset_version"):
        if key not in run:
            raise PacketValidationError(f"run.{key} is required")
    source = _require_mapping(packet, "source")
    for key in ("kind", "dataset_hash", "capabilities"):
        if key not in source:
            raise PacketValidationError(f"source.{key} is required")
    capabilities = _require_mapping(source, "capabilities")
    for key in ("stable_transaction_ids", "settlement_state", "report_exclusion"):
        if not isinstance(capabilities.get(key), bool):
            raise PacketValidationError(f"source.capabilities.{key} must be boolean")

    summary = _require_mapping(packet, "summary")
    summary_keys = (
        "transaction_count",
        "eligible_transaction_count",
        "excluded_transaction_count",
        "finding_count",
        "category_proposal_count",
        "unresolved_category_count",
        "stale_account_count",
    )
    for key in summary_keys:
        if not isinstance(summary.get(key), int) or isinstance(summary.get(key), bool):
            raise PacketValidationError(f"summary.{key} must be an integer")

    transactions = packet.get("transactions")
    if not isinstance(transactions, list):
        raise PacketValidationError("transactions must be an array")
    transaction_ids = packet.get("transaction_ids")
    if not isinstance(transaction_ids, list) or transaction_ids != [
        item.get("transaction_id") for item in transactions
    ]:
        raise PacketValidationError("transaction_ids must match transactions")
    for index, transaction in enumerate(transactions):
        if not isinstance(transaction, Mapping):
            raise PacketValidationError(f"transactions[{index}] must be an object")
        for key in (
            "transaction_id",
            "posted_on",
            "account_name",
            "merchant",
            "amount",
            "kind",
            "reason_codes",
            "provenance",
        ):
            if key not in transaction:
                raise PacketValidationError(f"transactions[{index}].{key} is required")
        flags = transaction.get("flags")
        if not isinstance(flags, Mapping):
            raise PacketValidationError(f"transactions[{index}].flags must be an object")
        if not isinstance(flags.get("projected"), bool):
            raise PacketValidationError(f"transactions[{index}].flags.projected must be boolean")

    for key in ("excluded_transactions", "findings", "category_proposals", "examples"):
        if not isinstance(packet.get(key), list):
            raise PacketValidationError(f"{key} must be an array")
    if not isinstance(packet.get("limitations"), list):
        raise PacketValidationError("limitations must be an array")
    if not isinstance(packet.get("policy_references"), list):
        raise PacketValidationError("policy_references must be an array")
    _validate_examples(packet["examples"])
    _assert_no_forbidden_keys(packet)
    try:
        json.dumps(packet, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise PacketValidationError("packet contains a non-JSON value") from exc


def write_packet(packet: Mapping[str, Any], path: Path) -> None:
    """Validate and write a canonical, newline-terminated JSON packet."""
    validate_packet(packet)
    artifacts.secure_write_text(
        path,
        json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
