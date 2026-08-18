import pytest

from profile_update import update

PROFILE = {"email": "a@b.com", "age": 30}


def test_update_email():
    assert update(PROFILE, email="C@d.com")["email"] == "c@d.com"


@pytest.mark.parametrize("email", ["nope", "@example.com", "user@"])
def test_update_rejects_the_same_emails_signup_does(email):
    with pytest.raises(ValueError):
        update(PROFILE, email=email)
