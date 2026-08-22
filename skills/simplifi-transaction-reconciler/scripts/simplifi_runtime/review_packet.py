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
from .subscriptions import FINDING_KINDS

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
_MONEY_EVIDENCE_FIELDS = {
    "amount",
    "annual_impact",
    "current",
    "median",
    "monthly",
    "now",
    "previous",
    "previous_typical",
    "projected_charge",
}
#: Evidence sub-objects whose every value is a money fact, whatever it is
#: called. A recurring finding names its own amounts — `previous`, `current`,
#: `projected_charge` today, something else the next time a check is added —
#: and a money fact that validation does not recognize as money is a money fact
#: nothing checks.
_MONEY_EVIDENCE_CONTAINERS = {"amounts"}

#: Exactly what a packet transaction may carry. Validating *presence* was never
#: enough: the required-key check passed on a transaction that also carried
#: `payee_raw` alongside them, and only the separate forbidden-key scan caught
#: that — which means a sensitive field nobody thought to forbid by name would
#: have travelled. An allowlist inverts the burden: a field is in the contract
#: or it is not in the packet.
_TRANSACTION_KEYS = {
    "account_name",
    "amount",
    "category",
    "flags",
    "inferred_category",
    "kind",
    "match_state",
    "merchant",
    "posted_on",
    "provenance",
    "reason_codes",
    "transacted_on",
    "transaction_id",
    "transaction_state",
}
_TRANSACTION_REQUIRED = {
    "account_name",
    "amount",
    "flags",
    "kind",
    "merchant",
    "posted_on",
    "provenance",
    "reason_codes",
    "transaction_id",
}
_MERCHANT_KEYS = {"canonical", "display", "normalized"}
_MONEY_KEYS = {"currency", "currency_exponent", "minor_units"}
_FLAG_KEYS = {"foreign_charge", "projected", "recurring", "reviewed", "split", "uncategorized"}
_PROVENANCE_KEYS = {
    "algorithm_version",
    "run_id",
    "ruleset_version",
    "source_hash",
    "transaction_version_id",
}
_FINDING_KEYS = {
    "confidence",
    "confidence_basis",
    "evidence",
    "policy_references",
    "priority",
    "reason_codes",
    "scope",
    "transaction_id",
    "transaction_ids",
}
_PROPOSAL_KEYS = {
    "category",
    "confidence",
    "evidence",
    "policy_references",
    "reason_codes",
    "transaction_id",
}
_EXCLUDED_KEYS = {"reason_codes", "transaction_id"}
#: A merchant-series finding's evidence, field by field.
_SERIES_EVIDENCE_KEYS = {
    "amounts",
    "annual_impact",
    "detail",
    "facts",
    "kind",
    "merchant",
    "series",
}
#: `amounts` and `facts` are kind-specific: a twin carries facts and no
#: amounts, a ghost carries both. Everything else is always present.
_SERIES_EVIDENCE_REQUIRED = {"annual_impact", "detail", "kind", "merchant", "series"}
_SERIES_ENTRY_KEYS = {
    "account_name",
    "interval_days",
    "last_charge",
    "merchant",
    "monthly",
    "transaction_ids",
}


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


def transaction_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map a stored row to the packet's deliberately small transaction shape.

    Every field here comes from `evidence`, so the packet, the report and the
    model payload describe the same transaction the same way. This function
    previously re-derived the merchant display, the safe account name and the
    currency exponent itself, and the report derived all three differently.

    Public because the HTML report renders through it. Two artifacts that
    select and format their own fields will eventually disagree about one, and
    a reader comparing a report against the packet it was generated with has no
    way to tell which is right. Deriving one from the other's projection makes
    the disagreement unrepresentable rather than merely unlikely.
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


#: Retained for existing callers and tests; `transaction_view` is the name.
_transaction = transaction_view


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


def _money_fact(money: Money) -> dict[str, Any]:
    """The packet's money shape: minor units, currency, and its exponent."""
    return {
        "minor_units": money.minor_units,
        "currency": money.currency,
        "currency_exponent": money.exponent,
    }


def _subscription_findings(subscription_findings: list[Any]) -> list[dict[str, Any]]:
    """Render recurring results. No recurring semantics live here.

    Everything below is a transcription of the result object: its kind, its
    series references, its money facts. The packet used to reconstruct meaning
    instead — it looked up a member transaction to guess the finding's
    currency, and anything not in `annual_impact` was only available inside the
    `detail` sentence. Both artifacts now render the same structure, so they
    cannot state a figure differently.
    """
    findings = []
    for finding in subscription_findings:
        evidence: dict[str, Any] = {
            "kind": _string(finding.kind),
            "merchant": _string(finding.merchant),
            "detail": _string(finding.detail),
            "annual_impact": _money_fact(finding.annual_impact),
            "series": [
                {
                    "merchant": _string(ref.merchant),
                    # The same sentinel transaction evidence uses. An empty
                    # string reads as malformed evidence rather than as an
                    # account the source never named, and a consumer cannot
                    # tell the two apart.
                    "account_name": _string(ref.account) or UNKNOWN_ACCOUNT,
                    "transaction_ids": sorted(str(txid) for txid in ref.transaction_ids),
                    "monthly": _money_fact(ref.monthly),
                    "interval_days": ref.interval_days,
                    "last_charge": _string(ref.last_charge) if ref.last_charge else None,
                }
                for ref in finding.series
            ],
        }
        if finding.amounts:
            evidence["amounts"] = {
                str(name): _money_fact(money) for name, money in sorted(finding.amounts.items())
            }
        if finding.facts:
            evidence["facts"] = {
                str(name): _json_safe(value) for name, value in sorted(finding.facts.items())
            }
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
                "reason_codes": [f"subscription:{finding.kind}"],
                "evidence": evidence,
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
    findings.extend(_subscription_findings(list(subscription_findings or [])))
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
        "transactions": [transaction_view(row) for row in eligible_rows],
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
    assert_no_sensitive_values(packet, rows)
    return packet


#: Row columns whose *value* must not appear anywhere in a packet, whatever it
#: is called there. The key-name scan cannot catch these: a raw descriptor that
#: arrived as `merchant.display`, or an account ID rendered as `account_name`,
#: sits under a permitted key and passes every structural check.
_SENSITIVE_COLUMNS = ("account_id", "payee_raw", "source_path", "original_amount")

#: The only column whose value may be *contained in* something we publish.
#:
#: A foreign charge's `original_amount` of `2.90` sits inside the
#: issuer-converted `-2.90` the packet legitimately states, and refusing that
#: would fail every foreign transaction over a value present only because the
#: amount is. Nothing else earns the exemption, and extending it to the
#: identifiers is how one escapes: an account genuinely named
#: `Checking acct-99887766` would make its own provider ID a substring of a
#: publishable value, and the ID would then travel inside `account_name` with
#: the check reporting success.
#:
#: For every other column the exemption is exact equality, which is all the
#: real case needs — a descriptor that normalization found nothing to strip
#: *equals* its merchant name rather than sitting inside it, and a stripped
#: name is shorter than the descriptor it came from, never longer.
_SUBSTRING_EXEMPT = ("original_amount",)

#: Below this a value is not identifying and collides with ordinary prose. The
#: same threshold `egress` uses, for the same reason: a three-character token
#: proves nothing and would fail every packet.
_MIN_SENSITIVE_LENGTH = 4


def assert_no_sensitive_values(
    packet: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> None:
    """Check the finished packet against the rows it was built from.

    The structural contract says which *fields* may appear. This asks the
    different question of whether a forbidden *value* ended up in the document
    anyway — through a rule's evidence dictionary, a curated example, a legacy
    row whose `payee_display` was really the bank descriptor, or a future
    mapping change. It is what keeps the allowlist a guarantee rather than a
    convention, and it runs on the exact text that is about to be written.

    Modelled on `egress.assert_payload_is_permitted`, deliberately: the packet
    and the model payload are two agent-facing artifacts, and a value unsafe
    for one is unsafe for the other. A packet that refused less than the
    payload would be the softer of two doors into the same room.

    A forbidden value that IS a value we are entitled to publish is not a
    finding — a descriptor that normalization found nothing to strip equals its
    own merchant name, and refusing that would fail the simplest merchants.
    That exemption is equality for every column but `original_amount`; see
    `_SUBSTRING_EXEMPT` for why widening it would open the door it closes.
    """
    document = json.dumps(packet, ensure_ascii=False, sort_keys=True)
    for row in rows:
        permitted = _publishable_values(row)
        for column in _SENSITIVE_COLUMNS:
            value = row.get(column)
            if value is None:
                continue
            text = str(value).strip()
            if len(text) < _MIN_SENSITIVE_LENGTH:
                continue
            if _is_publishable(text, column, permitted):
                continue
            if text in document:
                raise PacketValidationError(
                    f"packet contains {column}, which is not publishable "
                    f"(transaction {row.get('transaction_id', 'unknown')})"
                )


def _is_publishable(text: str, column: str, permitted: set[str]) -> bool:
    """Whether a forbidden value is accounted for by one we may publish."""
    if text in permitted:
        return True
    if column in _SUBSTRING_EXEMPT:
        return any(text in value for value in permitted)
    return False


def _publishable_values(row: Mapping[str, Any]) -> set[str]:
    """Exactly what this row is entitled to contribute to a packet."""
    evidence = evidence_from_row(row)
    values = {
        evidence.merchant.safe_display(),
        evidence.merchant.normalized,
        evidence.merchant.canonical,
        evidence.account.display,
        evidence.money.formatted(),
        evidence.transaction_id,
    }
    return {value for value in values if value}


def _require_mapping(packet: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = packet.get(key)
    if not isinstance(value, Mapping):
        raise PacketValidationError(f"{key} must be an object")
    return value


def _object_at(parent: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise PacketValidationError(f"{path}.{key} must be an object")
    return value


def _check_keys(
    value: Mapping[str, Any],
    path: str,
    allowed: set[str],
    required: set[str] | None = None,
) -> None:
    keys = {str(key) for key in value}
    unknown = sorted(keys - allowed)
    if unknown:
        raise PacketValidationError(f"{path} contains unsupported fields: {unknown}")
    missing = sorted((required if required is not None else allowed) - keys)
    if missing:
        raise PacketValidationError(f"{path} is missing required fields: {missing}")


def _check_int(value: Any, path: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PacketValidationError(f"{path} must be an integer")


def _check_string(value: Any, path: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str):
        raise PacketValidationError(f"{path} must be a string")


def _check_money(value: Any, path: str) -> None:
    """A money fact is three fields or it is not a money fact.

    Rejected rather than coerced. A packet whose amount is a bare float has
    already lost the distinction this runtime is built on — 1500 is ¥1,500 and
    $15.00, and a reader with only the number cannot tell which it was handed.
    """
    if not isinstance(value, Mapping):
        raise PacketValidationError(f"{path} must be an object")
    _check_keys(value, path, _MONEY_KEYS)
    _check_int(value.get("minor_units"), f"{path}.minor_units")
    _check_int(value.get("currency_exponent"), f"{path}.currency_exponent")
    exponent = value.get("currency_exponent")
    if isinstance(exponent, int) and not 0 <= exponent <= 4:
        raise PacketValidationError(
            f"{path}.currency_exponent is not a plausible ISO 4217 exponent"
        )
    currency = value.get("currency")
    _check_string(currency, f"{path}.currency")
    if not isinstance(currency, str) or not currency.strip():
        raise PacketValidationError(f"{path}.currency is required")


def _check_string_list(value: Any, path: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PacketValidationError(f"{path} must be an array of strings")


def _validate_transaction_shape(transaction: Any, path: str) -> None:
    if not isinstance(transaction, Mapping):
        raise PacketValidationError(f"{path} must be an object")
    _check_keys(transaction, path, _TRANSACTION_KEYS, _TRANSACTION_REQUIRED)

    _check_string(transaction.get("transaction_id"), f"{path}.transaction_id")
    _check_string(transaction.get("posted_on"), f"{path}.posted_on")
    _check_string(transaction.get("transacted_on"), f"{path}.transacted_on", optional=True)
    _check_string(transaction.get("category"), f"{path}.category", optional=True)
    _check_string(transaction.get("inferred_category"), f"{path}.inferred_category", optional=True)
    _check_string(transaction.get("kind"), f"{path}.kind")

    account_name = transaction.get("account_name")
    _check_string(account_name, f"{path}.account_name")
    if not isinstance(account_name, str) or not account_name.strip():
        # An empty account is what the packet is supposed to be unable to say.
        # The unnamed case has its own word, and it is not the empty string.
        raise PacketValidationError(
            f"{path}.account_name must name the account or be {UNKNOWN_ACCOUNT!r}"
        )

    merchant = _object_at(transaction, "merchant", path)
    _check_keys(merchant, f"{path}.merchant", _MERCHANT_KEYS)
    for key in sorted(_MERCHANT_KEYS):
        _check_string(merchant.get(key), f"{path}.merchant.{key}")
    if not str(merchant.get("display") or "").strip():
        raise PacketValidationError(f"{path}.merchant.display must not be empty")

    _check_money(transaction.get("amount"), f"{path}.amount")

    flags = _object_at(transaction, "flags", path)
    # Every flag, not only `projected`. A packet that simply omitted a flag
    # would read as False to any consumer using `.get`, so a projection that
    # lost its marker in transit would be presented as a real charge.
    _check_keys(flags, f"{path}.flags", _FLAG_KEYS)
    for key in sorted(_FLAG_KEYS):
        if not isinstance(flags.get(key), bool):
            raise PacketValidationError(f"{path}.flags.{key} must be boolean")

    _check_string_list(transaction.get("reason_codes"), f"{path}.reason_codes")

    provenance = _object_at(transaction, "provenance", path)
    _check_keys(provenance, f"{path}.provenance", _PROVENANCE_KEYS)
    for key in ("algorithm_version", "ruleset_version", "source_hash"):
        _check_string(provenance.get(key), f"{path}.provenance.{key}")


def _validate_finding_shape(finding: Any, path: str) -> None:
    if not isinstance(finding, Mapping):
        raise PacketValidationError(f"{path} must be an object")
    _check_keys(finding, path, _FINDING_KEYS)
    _check_string(finding.get("transaction_id"), f"{path}.transaction_id", optional=True)
    _check_string_list(finding.get("transaction_ids"), f"{path}.transaction_ids")
    if not finding["transaction_ids"]:
        raise PacketValidationError(f"{path}.transaction_ids must name at least one transaction")
    _check_string(finding.get("scope"), f"{path}.scope")
    _check_string(finding.get("confidence_basis"), f"{path}.confidence_basis")
    _check_string_list(finding.get("reason_codes"), f"{path}.reason_codes")
    _check_string_list(finding.get("policy_references"), f"{path}.policy_references")
    for key in ("priority", "confidence"):
        value = finding.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise PacketValidationError(f"{path}.{key} must be a number or null")
    evidence = finding.get("evidence")
    if not isinstance(evidence, (Mapping, list)):
        raise PacketValidationError(f"{path}.evidence must be an object or an array")
    if finding.get("scope") == "merchant_series":
        _validate_series_evidence(evidence, f"{path}.evidence")
    for money_path, money in _money_facts(evidence, f"{path}.evidence"):
        _check_money(money, money_path)


def _validate_series_evidence(evidence: Any, path: str) -> None:
    """Check the recurring-finding shape, not just the money inside it.

    Money validation walks the tree looking for monetary key names, which says
    nothing about the structure carrying them: `series` set to a string, a
    series entry missing its transaction IDs, an unknown key, or a `facts`
    value that is not an object all crossed the file boundary unchallenged.
    The contract is an allowlist everywhere else in this packet; it is one here
    too.
    """
    if not isinstance(evidence, Mapping):
        raise PacketValidationError(f"{path} must be an object for a merchant_series finding")
    _check_keys(evidence, path, _SERIES_EVIDENCE_KEYS, _SERIES_EVIDENCE_REQUIRED)
    for key in ("kind", "merchant", "detail"):
        _check_string(evidence.get(key), f"{path}.{key}")
    if evidence["kind"] not in FINDING_KINDS:
        raise PacketValidationError(
            f"{path}.kind must be one of {list(FINDING_KINDS)}, found {evidence['kind']!r}"
        )
    _check_money(evidence.get("annual_impact"), f"{path}.annual_impact")

    series = evidence.get("series")
    if not isinstance(series, list) or not series:
        raise PacketValidationError(f"{path}.series must be a non-empty array")
    for index, entry in enumerate(series):
        entry_path = f"{path}.series[{index}]"
        if not isinstance(entry, Mapping):
            raise PacketValidationError(f"{entry_path} must be an object")
        _check_keys(entry, entry_path, _SERIES_ENTRY_KEYS)
        _check_string(entry.get("merchant"), f"{entry_path}.merchant")
        _check_string(entry.get("account_name"), f"{entry_path}.account_name")
        _check_string_list(entry.get("transaction_ids"), f"{entry_path}.transaction_ids")
        if not entry["transaction_ids"]:
            raise PacketValidationError(f"{entry_path}.transaction_ids must name a transaction")
        _check_money(entry.get("monthly"), f"{entry_path}.monthly")
        interval = entry.get("interval_days")
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise PacketValidationError(f"{entry_path}.interval_days must be a number")
        last_charge = entry.get("last_charge")
        if last_charge is not None:
            _check_string(last_charge, f"{entry_path}.last_charge")

    for optional, checker in (("amounts", _check_money), ("facts", None)):
        value = evidence.get(optional)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            raise PacketValidationError(f"{path}.{optional} must be an object")
        if checker is not None:
            for name, money in value.items():
                checker(money, f"{path}.{optional}.{name}")


def _money_facts(value: Any, path: str):
    """Every nested money-shaped fact, so malformed ones cannot hide in evidence."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in _MONEY_EVIDENCE_CONTAINERS and isinstance(item, Mapping):
                for name, money in item.items():
                    yield f"{path}.{key}.{name}", money
            elif str(key) in _MONEY_EVIDENCE_FIELDS:
                yield f"{path}.{key}", item
            else:
                yield from _money_facts(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _money_facts(item, f"{path}[{index}]")


def _validate_proposal_shape(proposal: Any, path: str) -> None:
    if not isinstance(proposal, Mapping):
        raise PacketValidationError(f"{path} must be an object")
    _check_keys(proposal, path, _PROPOSAL_KEYS)
    _check_string(proposal.get("transaction_id"), f"{path}.transaction_id")
    _check_string(proposal.get("category"), f"{path}.category", optional=True)
    _check_string_list(proposal.get("reason_codes"), f"{path}.reason_codes")
    _check_string_list(proposal.get("policy_references"), f"{path}.policy_references")
    confidence = proposal.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise PacketValidationError(f"{path}.confidence must be a number or null")
        if not 0.0 <= float(confidence) <= 1.0:
            raise PacketValidationError(f"{path}.confidence must be between 0 and 1")
    if not isinstance(proposal.get("evidence"), Mapping):
        raise PacketValidationError(f"{path}.evidence must be an object")


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
        _validate_transaction_shape(transaction, f"transactions[{index}]")

    for key in ("excluded_transactions", "findings", "category_proposals", "examples"):
        if not isinstance(packet.get(key), list):
            raise PacketValidationError(f"{key} must be an array")
    _check_string_list(packet.get("limitations"), "limitations")
    if not isinstance(packet.get("policy_references"), list):
        raise PacketValidationError("policy_references must be an array")

    for index, excluded in enumerate(packet["excluded_transactions"]):
        path = f"excluded_transactions[{index}]"
        if not isinstance(excluded, Mapping):
            raise PacketValidationError(f"{path} must be an object")
        _check_keys(excluded, path, _EXCLUDED_KEYS)
        _check_string(excluded.get("transaction_id"), f"{path}.transaction_id")
        _check_string_list(excluded.get("reason_codes"), f"{path}.reason_codes")
    for index, finding in enumerate(packet["findings"]):
        _validate_finding_shape(finding, f"findings[{index}]")
    for index, proposal in enumerate(packet["category_proposals"]):
        _validate_proposal_shape(proposal, f"category_proposals[{index}]")

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
