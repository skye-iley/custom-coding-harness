import pytest

from solution import is_balanced


@pytest.mark.parametrize("s, expected", [
    ("", True),
    ("()", True),
    ("([{}])", True),
    ("(a + [b * {c}])", True),
    ("(", False),
    ("]", False),
    ("([)]", False),
    ("((()", False),
    ("no brackets here", True),
])
def test_is_balanced(s, expected):
    assert is_balanced(s) is expected
