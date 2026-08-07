import pytest
from simplifi_runtime.sources.api_source import ApiError, SimplifiApiClient, SimplifiApiSource
from simplifi_runtime.sources.csv_source import SimplifiCsvSource


def test_non_advancing_pagination_cursor_fails_closed():
    client = object.__new__(SimplifiApiClient)
    pages = iter(
        [
            {"resources": [{"id": "txn-1"}], "metaData": {"nextLink": "/transactions?after=1"}},
            {"resources": [{"id": "txn-1"}], "metaData": {"nextLink": "/transactions?after=2"}},
        ]
    )
    client.get = lambda _path, **_params: next(pages)

    with pytest.raises(ApiError, match="pagination cursor did not advance"):
        client.paginate("/transactions")


def test_malformed_collection_envelope_is_rejected():
    client = object.__new__(SimplifiApiClient)
    client.get = lambda _path, **_params: {"metaData": {}}

    with pytest.raises(ApiError, match="missing resources"):
        client.paginate("/transactions")


def test_duplicate_ids_within_page_are_rejected():
    client = object.__new__(SimplifiApiClient)
    client.get = lambda _path, **_params: {
        "resources": [{"id": "txn-1"}, {"id": "txn-1"}],
        "metaData": {},
    }

    with pytest.raises(ApiError, match="duplicate resource id within page"):
        client.paginate("/transactions")


def test_incomplete_transaction_record_is_rejected():
    source = object.__new__(SimplifiApiSource)

    with pytest.raises(ApiError, match=r"missing required field\(s\): amount"):
        source._to_record({"id": "txn-1", "postedOn": "2026-08-01"}, {}, {}, set())


def test_malformed_api_posted_on_date_is_rejected():
    with pytest.raises(ApiError, match="invalid postedOn date"):
        SimplifiApiSource._validate_transaction(
            {
                "id": "txn-1",
                "amount": "10.00",
                "postedOn": "08/01/2026",
                "accountId": "account-1",
            }
        )


def test_deleted_transaction_becomes_a_tombstone():
    assert SimplifiApiSource._tombstone(
        {"id": "txn-1", "isDeleted": True, "modifiedAt": "2026-08-06T12:00:00Z"}
    ) == {
        "transaction_id": "txn-1",
        "is_deleted": True,
        "modified_at": "2026-08-06T12:00:00Z",
    }


def test_missing_api_exclusion_flag_fails_closed():
    source = object.__new__(SimplifiApiSource)
    record = source._to_record(
        {
            "id": "txn-1",
            "amount": "10.00",
            "postedOn": "2026-08-01",
            "accountId": "account-1",
            "payee": "Example Store",
            "coa": {"type": "CATEGORY", "id": "cat-1"},
        },
        {},
        {"cat-1": {"id": "cat-1", "name": "Shopping"}},
        set(),
    )

    assert record["exclusion_flag"] == 2
    assert record["poisons_statistics"] == 0
    assert record["review_eligible"] == 1
    assert "report_exclusion_unknown" in record["eligibility_reason_codes"]
    assert "report-exclusion state unavailable" in record["semantics_reasons"]


def test_api_record_requires_account_identity():
    with pytest.raises(ApiError, match="accountId"):
        SimplifiApiSource._validate_transaction(
            {"id": "txn-1", "amount": "10.00", "postedOn": "2026-08-01", "accountId": " "}
        )


def test_csv_record_is_review_eligible_without_settlement_state(tmp_path):
    path = tmp_path / "transactions.csv"
    path.write_text(
        "Date,Account,Reviewed,Payee,Category,Attachments,Exclusion,Recurring,Amount\n"
        '"Aug 1, 2026",Checking,Yes,Example Store,Shopping,,No,No,-10.00\n',
        encoding="utf-8",
    )

    record = SimplifiCsvSource(path).fetch()[0]

    assert record["review_eligible"] == 1
    assert record["eligibility_reason_codes"] == "missing_optional_field,eligible"
