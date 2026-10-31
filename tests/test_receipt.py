from receipt import render


def test_render_lists_items_and_total():
    text = render([("pen", 1.5), ("book", 8.5)])
    assert "pen" in text and "book" in text
    assert text.splitlines()[-1].split() == ["total", "10.00"]


def test_render_empty_cart_has_zero_total():
    assert render([]).splitlines()[-1].split() == ["total", "0.00"]
