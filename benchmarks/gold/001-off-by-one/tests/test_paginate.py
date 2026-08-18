from paginate import page_count, paginate

ROWS = list(range(10))


def test_first_page():
    assert paginate(ROWS, 0, 3) == [0, 1, 2]


def test_middle_page():
    assert paginate(ROWS, 2, 3) == [6, 7, 8]


def test_last_page():
    assert paginate(ROWS, 3, 3) == [9]


def test_page_count():
    assert page_count(10, 3) == 4
    assert page_count(9, 3) == 3
    assert page_count(0, 3) == 0
