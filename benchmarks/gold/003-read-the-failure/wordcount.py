"""Word counting."""

from collections import Counter


def tokenize(text):
    return [w.strip(".,!?").lower() for w in text.split() if w.strip(".,!?")]


def top_words(text, n):
    """The `n` most common words, as (word, count) pairs, most common first."""
    counts = Counter(tokenize(text))
    # BUG: `max()` over an empty sequence raises, so any caller that reaches
    # here with no words at all blows up several frames below where it looks.
    ranked = counts.most_common()
    cutoff = max(count for _word, count in ranked)
    return [pair for pair in ranked if pair[1] >= cutoff - n][:n]
