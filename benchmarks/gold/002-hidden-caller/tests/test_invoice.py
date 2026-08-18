from invoice import invoice_total, line_total, render_total


def test_line_total():
    assert line_total("2.50", 4) == 1000


def test_invoice_total():
    assert invoice_total([("2.50", 4), ("1.00", 2)]) == 1200


def test_render_total():
    assert render_total([("2.50", 4), ("1.00", 2)]) == "12.00"
