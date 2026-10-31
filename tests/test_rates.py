import pytest

from shop.rates import RATES, convert


def test_convert_known_code():
    assert convert(10.0, "C100") == 20.0


def test_convert_base_like_code():
    assert convert(10.0, "C000") == 10.0


def test_unknown_code_raises():
    with pytest.raises(KeyError):
        convert(1.0, "ZZZ")


def test_table_size():
    assert len(RATES) == 520
