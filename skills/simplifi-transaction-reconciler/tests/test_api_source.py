import pytest
from simplifi_runtime.sources.api_source import ApiError, SimplifiApiClient, SimplifiApiSource


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


def test_incomplete_transaction_record_is_rejected():
    source = object.__new__(SimplifiApiSource)

    with pytest.raises(ApiError, match=r"missing required field\(s\): amount"):
        source._to_record({"id": "txn-1", "postedOn": "2026-08-01"}, {}, {}, set())


def test_deleted_transaction_becomes_a_tombstone():
    assert SimplifiApiSource._tombstone(
        {"id": "txn-1", "isDeleted": True, "modifiedAt": "2026-08-06T12:00:00Z"}
    ) == {
        "transaction_id": "txn-1",
        "is_deleted": True,
        "modified_at": "2026-08-06T12:00:00Z",
    }
