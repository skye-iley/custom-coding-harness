import pytest

from solution import is_leap_year


@pytest.mark.parametrize("year, expected", [
    (2000, True),   # divisible by 400
    (1900, False),  # century, not divisible by 400
    (2024, True),   # divisible by 4, not a century
    (2023, False),  # not divisible by 4
    (2400, True),   # divisible by 400
])
def test_is_leap_year(year, expected):
    assert is_leap_year(year) is expected
