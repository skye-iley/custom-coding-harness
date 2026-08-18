"""Profile updates."""


def update(profile, email=None, age=None):
    if email is not None:
        # BUG: this copy of the email rule is weaker than signup's -- it accepts
        # "@example.com" and "user@", which register() rejects.
        if "@" not in email:
            raise ValueError("invalid email")
        profile = {**profile, "email": email.lower()}
    if age is not None:
        if not isinstance(age, int) or age < 13:
            raise ValueError("invalid age")
        profile = {**profile, "age": age}
    return profile
