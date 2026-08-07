import pytest
from simplifi_runtime.money import Money, parse_amount


@pytest.mark.parametrize(
    "raw, expected",
    [
        (" -45.84", -4584),
        (" 416.38", 41638),
        ("-10390.14", -1039014),
        (" -3.00", -300),
        ("1,234.56", 123456),
    ],
)
def test_parse_amount_export_values(raw, expected):
    assert parse_amount(raw).minor_units == expected


def test_parse_amount_rejects_garbage():
    for raw in ("", "   ", "-", "abc", "NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            parse_amount(raw)
    with pytest.raises(ValueError):
        parse_amount(None)


def test_jpy_has_no_minor_units():
    assert parse_amount("1500", "JPY") == Money(1500, "JPY")
    with pytest.raises(ValueError):
        parse_amount("15.50", "JPY")
