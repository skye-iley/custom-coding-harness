"""Money is stored in whole CENTS everywhere in this package."""


def parse_amount(text):
    """Parse "12.34" into cents (1234). Raises ValueError on junk."""
    text = text.strip()
    if not text:
        raise ValueError("empty amount")
    if "." not in text:
        return int(text) * 100
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    return int(whole) * 100 + int(frac)


def format_amount(cents):
    """Render cents as a plain decimal string."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"
