"""Pagination helpers."""


def page_count(total, size):
    """How many pages `total` rows need at `size` rows per page."""
    if size <= 0:
        raise ValueError("size must be positive")
    return (total + size - 1) // size


def paginate(rows, page, size):
    """Return the rows on `page` (0-based), at most `size` of them."""
    if size <= 0:
        raise ValueError("size must be positive")
    start = page * size
    return rows[start:start + size + 1]
