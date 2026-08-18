import pytest

from signup import register


def test_register_ok():
    assert register("A@b.com", 30) == {"email": "a@b.com", "age": 30}


@pytest.mark.parametrize("email", ["nope", "@example.com", "user@"])
def test_register_rejects_bad_email(email):
    with pytest.raises(ValueError):
        register(email, 30)
