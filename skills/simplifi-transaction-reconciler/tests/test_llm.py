import json

import pytest
from simplifi_runtime.llm import Usage, classify


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
