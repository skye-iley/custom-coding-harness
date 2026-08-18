import pytest

from solution import decode, encode


@pytest.mark.parametrize("raw, packed", [
    ("", ""),
    ("a", "a1"),
    ("aaabcc", "a3b1c2"),
    ("aabbcc", "a2b2c2"),
    ("wwwwwwwwwwwwwwww", "w16"),
])
def test_encode(raw, packed):
    assert encode(raw) == packed


@pytest.mark.parametrize("raw, packed", [
    ("", ""),
    ("a", "a1"),
    ("aaabcc", "a3b1c2"),
    ("aabbcc", "a2b2c2"),
    ("wwwwwwwwwwwwwwww", "w16"),
])
def test_decode(raw, packed):
    assert decode(packed) == raw


def test_round_trip_is_stable_for_arbitrary_text():
    text = "mississippi river"
    assert decode(encode(text)) == text
