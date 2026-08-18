from solution import word_frequencies


def test_simple_sentence():
    assert word_frequencies("the cat sat on the mat") == {
        "the": 2, "cat": 1, "sat": 1, "on": 1, "mat": 1,
    }


def test_case_is_ignored():
    assert word_frequencies("The cat and the CAT") == {"the": 2, "cat": 2, "and": 1}


def test_punctuation_is_not_part_of_a_word():
    assert word_frequencies("cat, dog. cat!") == {"cat": 2, "dog": 1}


def test_empty_string():
    assert word_frequencies("") == {}


def test_repeated_whitespace_does_not_create_empty_words():
    assert word_frequencies("cat   dog\ncat") == {"cat": 2, "dog": 1}
