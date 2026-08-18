from solution import rotate_90


def test_2x2():
    assert rotate_90([[1, 2], [3, 4]]) == [[3, 1], [4, 2]]


def test_3x3():
    matrix = [
        [1, 2, 3],
        [4, 5, 6],
        [7, 8, 9],
    ]
    assert rotate_90(matrix) == [
        [7, 4, 1],
        [8, 5, 2],
        [9, 6, 3],
    ]


def test_1x1_is_unchanged():
    assert rotate_90([[5]]) == [[5]]


def test_four_rotations_return_to_the_start():
    matrix = [[1, 2], [3, 4]]
    result = matrix
    for _ in range(4):
        result = rotate_90(result)
    assert result == matrix


def test_the_input_is_not_mutated():
    matrix = [[1, 2], [3, 4]]
    original = [row[:] for row in matrix]
    rotate_90(matrix)
    assert matrix == original
