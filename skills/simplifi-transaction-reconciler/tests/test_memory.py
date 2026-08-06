from simplifi_runtime.memory import MerchantMemory


def _row(transaction_id: str, amount: int, category: str) -> dict:
    return {
        "transaction_id": transaction_id,
        "account_name": "Checking",
        "payee_canonical": "venmo",
        "amount_minor_units": amount,
        "category": category,
        "is_uncategorized": 0,
        "poisons_statistics": 0,
        "txn_state": "CLEARED",
    }


def test_memory_does_not_cross_train_credits_into_debits():
    memory = MerchantMemory()
    memory.train([_row(f"credit-{i}", 1000, "Income") for i in range(3)])

    proposal = memory.propose({**_row("debit-1", -1000, ""), "is_uncategorized": 1})

    assert proposal is None
