"""Invoice totals. Everything here is in CENTS."""

from money import format_amount, parse_amount


def line_total(unit_price_text, quantity):
    return parse_amount(unit_price_text) * quantity


def invoice_total(lines):
    """`lines` is a list of (unit_price_text, quantity)."""
    return sum(line_total(price, qty) for price, qty in lines)


def render_total(lines):
    # BUG: divides by 100 as well, so the total is rendered 100x too small.
    return format_amount(invoice_total(lines) / 100)
