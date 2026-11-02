"""Login checks."""
import hmac


def check_password(given: str, expected: str) -> bool:
    """Compare passwords in constant time."""
    return hmac.compare_digest(given.encode(), expected.encode())


def normalize_username(name: str) -> str:
    """Return the username trimmed and lower-cased for comparison."""
    return name.strip().lower()
