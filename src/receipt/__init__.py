"""Plain-text receipts."""
from tabulate import tabulate


def render(items: list[tuple[str, float]]) -> str:
    """Render (name, price) rows and a total line as a text table."""
    rows = [(name, f"{price:.2f}") for name, price in items]
    rows.append(("total", f"{sum(price for _, price in items):.2f}"))
    return tabulate(rows, headers=("item", "price"), disable_numparse=True)
