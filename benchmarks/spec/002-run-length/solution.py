"""Run-length encoding."""


def encode(s):
    """Compress consecutive runs of the same character.

    'aaabcc' -> 'a3b1c2'. Every run, including a run of length 1, is followed
    by its count. The empty string encodes as the empty string.
    """
    raise NotImplementedError


def decode(s):
    """Invert `encode`: 'a3b1c2' -> 'aaabcc'."""
    raise NotImplementedError
