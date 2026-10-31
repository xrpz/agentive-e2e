import pytest

from shop.auth import MIN_PASSWORD_LENGTH, check_password, validate_new_password


def test_new_password_at_minimum_is_accepted():
    validate_new_password("a" * MIN_PASSWORD_LENGTH)


def test_new_password_below_minimum_is_rejected():
    with pytest.raises(ValueError):
        validate_new_password("a" * (MIN_PASSWORD_LENGTH - 1))


def test_empty_new_password_is_rejected():
    with pytest.raises(ValueError):
        validate_new_password("")


def test_existing_short_password_still_checks():
    assert check_password("abc", "abc")
