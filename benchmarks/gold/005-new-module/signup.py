"""User signup."""


def register(email, age):
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("invalid email")
    if not isinstance(age, int) or age < 13:
        raise ValueError("invalid age")
    return {"email": email.lower(), "age": age}
