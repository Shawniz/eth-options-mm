from datetime import UTC, datetime

import pytest

from mm.data.instruments import parse_instrument


def test_parse_option_call():
    i = parse_instrument("ETH-28FEB25-4500-C")
    assert i.kind == "option"
    assert i.expiry == datetime(2025, 2, 28, 8, 0, tzinfo=UTC)
    assert i.strike_usd == 4500.0
    assert i.option_type == "C"


def test_parse_option_single_digit_day_put():
    i = parse_instrument("ETH-2JAN25-3325-P")
    assert i.expiry == datetime(2025, 1, 2, 8, 0, tzinfo=UTC)
    assert i.option_type == "P"
    assert i.strike_usd == 3325.0


def test_parse_decimal_strike():
    # Deribit writes decimal strikes with 'd' (e.g. 2d5 = 2.5)
    assert parse_instrument("ETH-2JAN25-2d5-C").strike_usd == 2.5


def test_parse_future():
    i = parse_instrument("ETH-27JUN25")
    assert i.kind == "future"
    assert i.expiry == datetime(2025, 6, 27, 8, 0, tzinfo=UTC)
    assert i.strike_usd is None and i.option_type is None


def test_parse_perpetual():
    i = parse_instrument("ETH-PERPETUAL")
    assert i.kind == "perpetual"
    assert i.expiry is None


@pytest.mark.parametrize(
    "name",
    ["BTC-28FEB25-90000-C", "ETH-30FEB25-4500-C", "ETH-28FOO25-4500-C", "ETH-28FEB25-4500-X", "x"],
)
def test_parse_rejects_bad_names(name):
    with pytest.raises(ValueError):
        parse_instrument(name)


def test_expiry_matches_deribit_timestamp():
    # expiration_timestamp from history get_instruments for ETH-29MAR19-110-C
    assert parse_instrument("ETH-29MAR19-110-C").expiry_ms == 1553846400000
