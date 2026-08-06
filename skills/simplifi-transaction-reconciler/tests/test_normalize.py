import csv

from simplifi_runtime.normalize import normalize
from simplifi_runtime.sources.csv_source import SimplifiCsvSource


def test_original_charge_prefix_is_stripped_with_provenance():
    descriptor = normalize("2.90 Euro Tmb Bus Transit")

    assert descriptor.normalized == "Tmb Bus Transit"
    assert descriptor.canonical == "tmb_bus_transit"
    assert descriptor.original_currency == "EUR"
    assert descriptor.original_amount == "2.90"
    assert "strip_original_charge_prefix" in descriptor.rules_applied


def test_original_currency_does_not_change_csv_transaction_currency(tmp_path):
    path = tmp_path / "transactions.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "Date",
                "Account",
                "Reviewed",
                "Payee",
                "Category",
                "Attachments",
                "Exclusion",
                "Recurring",
                "Amount",
            ]
        )
        writer.writerow(
            [
                "Jul 21, 2026",
                "Alliant Visa",
                "no",
                "2.90 Euro Tmb Bus Transit",
                "Auto & Transport",
                "no",
                "no",
                "no",
                " -3.33",
            ]
        )

    record = SimplifiCsvSource(path).fetch()[0]

    assert record["currency"] == "USD"
    assert record["amount_minor_units"] == -333
    assert record["original_currency"] == "EUR"
    assert record["is_foreign_charge"] == 1
    assert "strip_original_charge_prefix" in record["norm_rules_applied"]


def test_same_merchant_different_original_amounts_share_identity():
    first = normalize("0.60 Euro Mercadona Calella")
    second = normalize("15.64 Euro Mercadona Calella")

    assert first.canonical == second.canonical == "mercadona_calella"


def test_processor_and_card_noise_are_removed():
    card = normalize(
        "Loy*themedicalgroupnj, 908-520-1927, NJ "
        "(Card Transaction ID: nobdyq7)"
    )

    assert "nobdyq7" not in card.normalized
    assert "908-520-1927" not in card.normalized
    assert "themedicalgroupnj" in card.normalized.lower()
    assert normalize("SQ *BLUEBIRD COFFEE").canonical == "bluebird_coffee"
    assert normalize("TST* Some Diner").canonical == "some_diner"


def test_normalization_preserves_raw_and_never_returns_empty_identity():
    raw = "SQ *BLUEBIRD COFFEE 1234 NJ"
    assert normalize(raw).raw == raw

    for pathological in ("", "   ", "***", "12345678", "NJ"):
        assert normalize(pathological).canonical
