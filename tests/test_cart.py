import pytest

from shop.auth import check_password
from shop.cart import item_count, total


def test_total_no_discount():
    assert total([1.0, 2.5]) == 3.5


def test_total_discount():
    assert total([10.0], 0.1) == 9.0


def test_bad_discount():
    with pytest.raises(ValueError):
        total([1.0], 2.0)


def test_check_password():
    assert check_password("a", "a")
    assert not check_password("a", "b")


def test_item_count():
    assert item_count([]) == 0
    assert item_count([1.0, 2.5, 2.5]) == 3
