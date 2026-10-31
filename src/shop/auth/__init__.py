"""Login checks."""
import hmac

MIN_PASSWORD_LENGTH = 8


def check_password(given: str, expected: str) -> bool:
    """Compare passwords in constant time."""
    return hmac.compare_digest(given.encode(), expected.encode())


def validate_new_password(candidate: str) -> None:
    """Reject a new password shorter than MIN_PASSWORD_LENGTH.

    Only for passwords being set: check_password must keep accepting existing short passwords.
    """
    if len(candidate) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
