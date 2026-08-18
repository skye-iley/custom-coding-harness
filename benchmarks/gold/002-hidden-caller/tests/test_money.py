from money import format_amount, parse_amount


def test_parse_whole():
    assert parse_amount("12") == 1200


def test_parse_decimal():
    assert parse_amount("12.34") == 1234


def test_format():
    assert format_amount(1234) == "12.34"
    assert format_amount(5) == "0.05"
