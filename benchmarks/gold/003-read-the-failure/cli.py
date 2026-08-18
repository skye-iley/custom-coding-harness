"""A tiny word-frequency CLI."""

import sys

from wordcount import top_words


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    text = " ".join(argv)
    lines = []
    for word, count in top_words(text, 3):
        lines.append(f"{word}: {count}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    print(main())
