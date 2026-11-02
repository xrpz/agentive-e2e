from shop.auth import normalize_username


def test_normalize_username():
    assert normalize_username("  Alice ") == "alice"
