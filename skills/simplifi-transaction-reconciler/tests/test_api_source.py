from typing import Any, cast

import pytest
from simplifi_runtime.sources.api_source import ApiError, SimplifiApiClient, SimplifiApiSource
from simplifi_runtime.sources.csv_source import SimplifiCsvSource


def _client_serving(*pages: dict) -> SimplifiApiClient:
    """A client whose `get` replays canned envelopes, with no network."""
    client = object.__new__(SimplifiApiClient)
    served = iter(pages)
    client.get = cast(Any, lambda _path, **_params: next(served))
    return client


def test_walk_reports_the_response_level_as_of():
    client = _client_serving(
        {
            "resources": [{"id": "txn-1"}],
            "metaData": {"asOf": "2026-08-06T12:00:00Z"},
        }
    )

    assert client.walk("/transactions").as_of == "2026-08-06T12:00:00Z"


def test_as_of_wins_over_records_modified_later_than_it():
    """The marker is authoritative even when a row claims a newer modifiedAt.

    Advancing to max(modifiedAt) here would move the cursor past
    12:00:00 to 18:00:00 and skip anything the server had not yet published
    in between. asOf is the server's own coverage claim; the rows are not.
    """
    client = _client_serving(
        {
            "resources": [
                {"id": "txn-1", "modifiedAt": "2026-08-06T18:00:00Z"},
                {"id": "txn-2", "modifiedAt": "2026-08-01T00:00:00Z"},
            ],
            "metaData": {"asOf": "2026-08-06T12:00:00Z"},
        }
    )

    assert client.walk("/transactions").as_of == "2026-08-06T12:00:00Z"


def test_as_of_is_taken_from_the_first_page_of_a_multi_page_walk():
    """A multi-page walk only provably covers the instant it started from."""
    client = _client_serving(
        {
            "resources": [{"id": "txn-1"}],
            "metaData": {"asOf": "2026-08-06T12:00:00Z", "nextLink": "/transactions?after=1"},
        },
        {
            "resources": [{"id": "txn-2"}],
            "metaData": {"asOf": "2026-08-06T12:05:00Z"},
        },
    )

    result = client.walk("/transactions")

    assert [r["id"] for r in result.resources] == ["txn-1", "txn-2"]
    assert result.as_of == "2026-08-06T12:00:00Z"


def test_empty_successful_response_still_carries_as_of():
    """A "nothing changed" response is a real answer, and its marker is usable."""
    client = _client_serving({"resources": [], "metaData": {"asOf": "2026-08-06T12:00:00Z"}})

    result = client.walk("/transactions")

    assert result.resources == []
    assert result.as_of == "2026-08-06T12:00:00Z"


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"asOf": None},
        {"asOf": ""},
        {"asOf": "   "},
        {"asOf": 1754481600},
        {"asOf": ["2026-08-06T12:00:00Z"]},
    ],
    ids=["absent", "null", "empty", "blank", "numeric", "list"],
)
def test_missing_or_malformed_as_of_yields_no_marker(metadata):
    client = _client_serving({"resources": [{"id": "txn-1"}], "metaData": metadata})

    result = client.walk("/transactions")

    assert [r["id"] for r in result.resources] == ["txn-1"]
    assert result.as_of is None


def test_api_error_during_the_walk_yields_no_marker_at_all():
    """A failed fetch has no marker to offer, so nothing can advance."""
    client = object.__new__(SimplifiApiClient)
    client.get = cast(Any, _raise_api_error)

    with pytest.raises(ApiError, match="/transactions returned 500"):
        client.walk("/transactions")


def _raise_api_error(_path, **_params):
    raise ApiError("/transactions returned 500")


def test_source_exposes_as_of_after_fetch():
    source = object.__new__(SimplifiApiSource)
    source.date_on_after = None
    source.modified_after = None
    source.as_of = None
    source.client = _client_serving(
        {"resources": [], "metaData": {}},  # accounts
        {"resources": [], "metaData": {}},  # categories
        {
            "resources": [{"id": "txn-1", "isDeleted": True, "modifiedAt": "2026-08-06T18:00:00Z"}],
            "metaData": {"asOf": "2026-08-06T12:00:00Z"},
        },
    )

    records = source.fetch()

    assert [r["transaction_id"] for r in records] == ["txn-1"]
    assert source.as_of == "2026-08-06T12:00:00Z"


def test_non_advancing_pagination_cursor_fails_closed():
    client = object.__new__(SimplifiApiClient)
    pages = iter(
        [
            {"resources": [{"id": "txn-1"}], "metaData": {"nextLink": "/transactions?after=1"}},
            {"resources": [{"id": "txn-1"}], "metaData": {"nextLink": "/transactions?after=2"}},
        ]
    )
    client.get = cast(Any, lambda _path, **_params: next(pages))

    with pytest.raises(ApiError, match="pagination cursor did not advance"):
        client.paginate("/transactions")


def test_malformed_collection_envelope_is_rejected():
    client = object.__new__(SimplifiApiClient)
    client.get = cast(Any, lambda _path, **_params: {"metaData": {}})

    with pytest.raises(ApiError, match="missing resources"):
        client.paginate("/transactions")


def test_duplicate_ids_within_page_are_rejected():
    client = object.__new__(SimplifiApiClient)
    client.get = cast(
        Any,
        lambda _path, **_params: {
            "resources": [{"id": "txn-1"}, {"id": "txn-1"}],
            "metaData": {},
        },
    )

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
