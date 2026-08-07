import json

import pytest
from simplifi_runtime.llm import PROMPT_VERSION, Usage, build_prompt, classify


class FakeBackend:
    id = "fake"

    def __init__(self, payload):
        self.payload = payload

    def complete(self, system, user):
        del system, user
        return json.dumps(self.payload), Usage(requests=1)


def _rows():
    return [
        {
            "transaction_id": "txn-1",
            "payee_display": "Example",
            "amount_minor_units": -100,
            "account_name": "Checking",
            "posted_on": "2026-08-01",
        },
        {
            "transaction_id": "txn-2",
            "payee_display": "Other",
            "amount_minor_units": -200,
            "account_name": "Checking",
            "posted_on": "2026-08-02",
        },
    ]


@pytest.mark.parametrize(
    "results, message",
    [
        ([{"id": "txn-1", "category": "Shopping"}], "missing"),
        (
            [
                {"id": "txn-1", "category": "Shopping"},
                {"id": "txn-1", "category": "Shopping"},
            ],
            "duplicate",
        ),
        (
            [
                {"id": "txn-1", "category": "Shopping"},
                {"id": "txn-x", "category": "Shopping"},
            ],
            "unknown",
        ),
    ],
)
def test_classify_rejects_non_exact_batch_results(results, message):
    with pytest.raises(ValueError, match=message):
        classify(FakeBackend({"results": results}), _rows(), ["Shopping"], [], chunk_size=2)


@pytest.mark.parametrize("confidence", [-0.1, 1.1, "NaN", "Infinity"])
def test_classify_rejects_invalid_confidence(confidence):
    payload = {
        "results": [
            {"id": "txn-1", "category": "Shopping", "confidence": confidence},
            {"id": "txn-2", "category": "Shopping", "confidence": 0.5},
        ]
    }

    with pytest.raises(ValueError, match="confidence"):
        classify(FakeBackend(payload), _rows(), ["Shopping"], [], chunk_size=2)


def test_classification_proposals_include_prompt_provenance():
    payload = {
        "results": [
            {"id": "txn-1", "category": "Shopping"},
            {"id": "txn-2", "category": "Shopping"},
        ]
    }

    proposals, _, _ = classify(FakeBackend(payload), _rows(), ["Shopping"], [], chunk_size=2)

    assert proposals[0].prompt_version == PROMPT_VERSION
    assert proposals[0].prompt_hash
    assert proposals[0].prompt_hash == proposals[1].prompt_hash


def test_prompt_uses_curated_decision_examples_without_transaction_history():
    examples = [
        {
            "id": "judgment-1",
            "title": "Projected versus real subscription",
            "situation": "A recurring schedule looked too active.",
            "evidence": "Future rows were projections.",
            "proposal_or_escalation": "Separate forecast evidence.",
            "human_decision": "Review only settled activity.",
            "reusable_lesson": "A forecast is not a transaction.",
        }
    ]

    prompt = build_prompt(["Shopping"], examples, _rows())

    assert "CURATED JUDGMENT EXAMPLES" in prompt
    assert "Projected versus real subscription" in prompt
    assert "A forecast is not a transaction." in prompt
    assert "Example  (-1.00, Checking)" not in prompt
