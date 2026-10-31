"""Shopping cart totals."""


def total(prices: list[float], discount: float = 0.0) -> float:
    """Return the cart total after a fractional discount (0.0-1.0)."""
    if not 0.0 <= discount <= 1.0:
        raise ValueError("discount must be between 0 and 1")
    return round(sum(prices) * (1.0 - discount), 2)
