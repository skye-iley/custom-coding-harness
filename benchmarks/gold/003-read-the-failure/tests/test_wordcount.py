from wordcount import tokenize, top_words


def test_tokenize():
    assert tokenize("Hello, world!") == ["hello", "world"]


def test_top_words():
    pairs = top_words("a a a b b c", 2)
    assert pairs[0] == ("a", 3)
    assert pairs[1] == ("b", 2)
