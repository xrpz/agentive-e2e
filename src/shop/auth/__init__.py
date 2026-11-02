"""Login checks."""
import hmac


def check_password(given: str, expected: str) -> bool:
    """Compare passwords in constant time."""
    return hmac.compare_digest(given.encode(), expected.encode())
