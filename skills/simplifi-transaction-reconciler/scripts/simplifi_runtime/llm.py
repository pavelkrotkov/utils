"""LLM classification for the residue merchant memory cannot resolve.

Synchronous, not batched. The workloads are small enough that batching would
save little while adding durable state and orchestration.

Two backends behind one protocol. Both are kept and both stay exercised by the
eval harness; an unexercised fallback rots silently until the day you need it.

GUARDRAILS
  - the model never writes anything; it returns proposals
  - every returned category is validated against the supplied taxonomy and
    rejected outright if it is not a member — never coerced or fuzzy-matched
  - what may be sent is decided by `egress`, not here: this module renders the
    records it is handed and cannot reach back into a database row for more
  - nothing is transmitted without an explicit `--send`, and every payload is
    assembled and written out before the first request leaves
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from . import egress

CHUNK_SIZE = 40
REQUEST_TIMEOUT = 90
PROMPT_VERSION = "classification-prompt-v2"

SYSTEM_PROMPT = """\
You categorise personal financial transactions.

You will be given a fixed list of allowed categories and a batch of transactions.
For each transaction, choose the single best category FROM THE ALLOWED LIST.

Rules:
- Use only categories from the allowed list, copied exactly. Never invent one.
- If no category fits with reasonable confidence, return "category": null.
  A null is far more useful than a confident wrong answer.
- Many merchants here are non-US. Use the merchant name, the amount and the
  account to infer the nature of the purchase.
- confidence is 0.0-1.0 and should reflect genuine uncertainty.
- rationale is at most 12 words.

Return ONLY a JSON object of the form:
{"results": [{"id": "...", "category": "..." | null, "confidence": 0.0, "rationale": "..."}]}
"""


@dataclass
class Proposal:
    transaction_id: str
    category: str | None
    confidence: float
    rationale: str
    model: str
    rejected_category: str | None = None  # set when the model invented one
    prompt_version: str = PROMPT_VERSION
    prompt_hash: str = ""


@dataclass
class Payload:
    """One prepared request: the exact text, and how to read the answer back."""

    user: str
    batch: list[dict] = field(default_factory=list)
    #: surrogate id -> real transaction id, kept locally and never transmitted.
    surrogates: dict[str, str] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    def cost_usd(self, in_per_mtok: float, out_per_mtok: float) -> float:
        return (self.input_tokens * in_per_mtok + self.output_tokens * out_per_mtok) / 1e6


class Backend(Protocol):
    id: str

    def complete(self, system: str, user: str) -> tuple[str, Usage]: ...


def _post(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"{url} returned {exc.code}: {detail}") from exc


class LunaBackend:
    """GPT-5.6 Luna — the cheap tier, positioned for classification work."""

    id = "gpt-5.6-luna"
    price_in, price_out = 1.00, 6.00

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or "gpt-5.6-luna"

    def complete(self, system: str, user: str) -> tuple[str, Usage]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        data = _post(
            "https://api.openai.com/v1/chat/completions",
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        u = data.get("usage", {})
        return data["choices"][0]["message"]["content"], Usage(
            u.get("prompt_tokens", 0), u.get("completion_tokens", 0), 1
        )


class HaikuBackend:
    """Claude Haiku 4.5 — $1.00/$5.00 per Mtok."""

    id = "claude-haiku-4-5"
    price_in, price_out = 1.00, 5.00

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or "claude-haiku-4-5-20251001"

    def complete(self, system: str, user: str) -> tuple[str, Usage]:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        data = _post(
            "https://api.anthropic.com/v1/messages",
            {
                "model": self.model,
                "max_tokens": 4096,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "temperature": 0,
            },
            {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        u = data.get("usage", {})
        return data["content"][0]["text"], Usage(
            u.get("input_tokens", 0), u.get("output_tokens", 0), 1
        )


BACKENDS = {"luna": LunaBackend, "haiku": HaikuBackend}
REQUIRED_API_KEYS = {"luna": "OPENAI_API_KEY", "haiku": "ANTHROPIC_API_KEY"}


def build_prompt(taxonomy: list[str], examples: list[dict], records: Sequence[dict]) -> str:
    """User message. Taxonomy and examples first so the prefix is cacheable.

    Takes records already minimized by `egress.minimize`, not database rows.
    The distinction is the point: this function can only render the fields it
    is handed, so a column added to `transaction_version` later cannot reach a
    model by being picked up here.
    """
    lines = ["ALLOWED CATEGORIES:"]
    lines += [f"  {c}" for c in taxonomy]
    if examples:
        lines.append("")
        lines.append("CURATED JUDGMENT EXAMPLES (general guidance; never transaction history):")
        for e in examples:
            lines.append(f"  [{e['id']}] {e['title']}")
            lines.append(f"    situation: {e['situation']}")
            lines.append(f"    evidence: {e['evidence']}")
            lines.append(f"    proposal or escalation: {e['proposal_or_escalation']}")
            lines.append(f"    human decision: {e['human_decision']}")
            lines.append(f"    reusable lesson: {e['reusable_lesson']}")
    lines.append("")
    lines.append("TRANSACTIONS TO CATEGORISE:")
    for record in records:
        rendered = " | ".join(f"{key}={value}" for key, value in record.items())
        lines.append(f"  {rendered}")
    return "\n".join(lines)


def _parse(text: str, taxonomy: set[str], model: str) -> list[Proposal]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"model did not return JSON: {text[:200]!r}") from None
        payload = json.loads(text[start : end + 1])

    out: list[Proposal] = []
    for item in payload.get("results", []):
        category = item.get("category")
        rejected = None
        if category is not None and category not in taxonomy:
            # Not a member of the supplied taxonomy. Reject rather than coerce;
            # a plausible-looking invented category is worse than no answer.
            rejected, category = category, None
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("model returned a non-numeric confidence") from exc
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError(f"model returned invalid confidence: {confidence!r}")
        out.append(
            Proposal(
                transaction_id=str(item.get("id", "")),
                category=category,
                confidence=confidence,
                rationale=str(item.get("rationale", ""))[:120],
                model=model,
                rejected_category=rejected,
            )
        )
    return out


def _validate_batch_ids(proposals: list[Proposal], surrogates: Mapping[str, str]) -> None:
    """Reject partial, duplicate, or out-of-batch model responses.

    Compared against the surrogate IDs, which is what the model was shown and
    therefore all it can legitimately answer with. A response naming a real
    transaction ID would fail here — correctly, since it could not have learned
    one from the request.
    """
    expected = list(surrogates)
    actual = [proposal.transaction_id for proposal in proposals]
    if len(actual) != len(set(actual)):
        raise ValueError("model returned duplicate transaction IDs")
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        unknown = sorted(set(actual) - set(expected))
        detail = []
        if missing:
            detail.append(f"missing={missing}")
        if unknown:
            detail.append(f"unknown={unknown}")
        raise ValueError(
            "model did not return one result per transaction (" + ", ".join(detail) + ")"
        )


def build_payloads(
    rows: list[dict],
    taxonomy: list[str],
    examples: list[dict],
    *,
    chunk_size: int = CHUNK_SIZE,
    redact: Iterable[str] = (),
) -> list[Payload]:
    """Assemble every request, without sending any of them.

    Separated from `classify` so the exact bytes can be written to disk and
    read by a person *before* the decision to transmit, rather than the review
    artifact being a thing you get only when you choose not to send.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    payloads: list[Payload] = []
    for start in range(0, len(rows), chunk_size):
        batch = rows[start : start + chunk_size]
        minimized = egress.minimize(batch, redact)
        user = build_prompt(taxonomy, examples, minimized.records)
        # The check runs on the finished text, against the rows it came from —
        # after the taxonomy and the curated examples have been folded in, so
        # it covers what they contribute too.
        egress.assert_payload_is_permitted(user, batch, redact=redact)
        payloads.append(Payload(user=user, batch=batch, surrogates=minimized.surrogates))
    return payloads


def classify(
    backend: Backend,
    payloads: Sequence[Payload],
    taxonomy: list[str],
) -> tuple[list[Proposal], Usage]:
    """Send prepared payloads and return the proposals they produced.

    Takes payloads rather than rows: by the time anything is transmitted the
    caller has already assembled, checked, and written them out. There is no
    path through this function that builds a request the caller has not seen.
    """
    allowed = set(taxonomy)
    proposals: list[Proposal] = []
    total = Usage()

    for payload in payloads:
        text, usage = backend.complete(SYSTEM_PROMPT, payload.user)
        batch_proposals = _parse(text, allowed, backend.id)
        _validate_batch_ids(batch_proposals, payload.surrogates)
        prompt_hash = hashlib.sha256(
            f"{PROMPT_VERSION}\n{SYSTEM_PROMPT}\n{payload.user}".encode()
        ).hexdigest()
        for proposal in batch_proposals:
            # Answers come back keyed by surrogate; restore the real ID here,
            # at the boundary, so nothing downstream has to know surrogates
            # existed.
            proposal.transaction_id = payload.surrogates[proposal.transaction_id]
            proposal.prompt_version = PROMPT_VERSION
            proposal.prompt_hash = prompt_hash
        proposals.extend(batch_proposals)
        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens
        total.requests += usage.requests

    return proposals, total
