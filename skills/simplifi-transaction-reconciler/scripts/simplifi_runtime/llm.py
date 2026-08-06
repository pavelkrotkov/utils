"""LLM classification for the residue merchant memory cannot resolve.

Synchronous, not batched. The workloads are small enough that batching would
save little while adding durable state and orchestration.

Two backends behind one protocol. Both are kept and both stay exercised by the
eval harness; an unexercised fallback rots silently until the day you need it.

GUARDRAILS
  - the model never writes anything; it returns proposals
  - every returned category is validated against the supplied taxonomy and
    rejected outright if it is not a member — never coerced or fuzzy-matched
  - transactions are sent with payee, amount, account and date only
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .semantics import is_settled

CHUNK_SIZE = 40
REQUEST_TIMEOUT = 90

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


def build_prompt(taxonomy: list[str], examples: list[dict], batch: list[dict]) -> str:
    """User message. Taxonomy and examples first so the prefix is cacheable."""
    lines = ["ALLOWED CATEGORIES:"]
    lines += [f"  {c}" for c in taxonomy]
    if examples:
        lines.append("")
        lines.append("EXAMPLES FROM THIS USER'S OWN HISTORY:")
        for e in examples:
            lines.append(f"  {e['payee']}  ({e['amount']:.2f}, {e['account']}) -> {e['category']}")
    lines.append("")
    lines.append("TRANSACTIONS TO CATEGORISE:")
    for r in batch:
        lines.append(
            f"  id={r['transaction_id']} | payee={r['payee_display']} | "
            f"amount={r['amount_minor_units'] / 100:.2f} | account={r['account_name']} | "
            f"date={r['posted_on']}"
        )
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
        out.append(
            Proposal(
                transaction_id=str(item.get("id", "")),
                category=category,
                confidence=float(item.get("confidence", 0.0)),
                rationale=str(item.get("rationale", ""))[:120],
                model=model,
                rejected_category=rejected,
            )
        )
    return out


def _validate_batch_ids(proposals: list[Proposal], batch: list[dict]) -> None:
    """Reject partial, duplicate, or out-of-batch model responses."""
    expected = [str(row["transaction_id"]) for row in batch]
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


def classify(
    backend: Backend,
    rows: list[dict],
    taxonomy: list[str],
    examples: list[dict],
    *,
    chunk_size: int = CHUNK_SIZE,
    dry_run: bool = False,
) -> tuple[list[Proposal], Usage, list[str]]:
    """Classify rows. With dry_run=True, returns the prompts instead of calling out."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    allowed = set(taxonomy)
    proposals: list[Proposal] = []
    total = Usage()
    prompts: list[str] = []

    for i in range(0, len(rows), chunk_size):
        batch = rows[i : i + chunk_size]
        user = build_prompt(taxonomy, examples, batch)
        prompts.append(user)
        if dry_run:
            continue
        text, usage = backend.complete(SYSTEM_PROMPT, user)
        batch_proposals = _parse(text, allowed, backend.id)
        _validate_batch_ids(batch_proposals, batch)
        proposals.extend(batch_proposals)
        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens
        total.requests += usage.requests

    return proposals, total, prompts


def build_examples(rows: list[dict], per_category: int = 1, limit: int = 24) -> list[dict]:
    """Few-shot pairs drawn from the user's own categorised, non-transfer history."""
    seen: dict[str, int] = {}
    out: list[dict] = []
    for r in sorted(rows, key=lambda x: x["posted_on"], reverse=True):
        if r["poisons_statistics"] or r["is_uncategorized"] or not is_settled(r):
            continue
        cat = (r["category"] or "").strip()
        if not cat or seen.get(cat, 0) >= per_category:
            continue
        seen[cat] = seen.get(cat, 0) + 1
        out.append(
            {
                "payee": r["payee_display"],
                "amount": r["amount_minor_units"] / 100,
                "account": r["account_name"],
                "category": cat,
            }
        )
        if len(out) >= limit:
            break
    return out
