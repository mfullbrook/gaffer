"""Text helpers."""


def shout(text: str) -> str:
    """Return text upper-cased."""
    return text.upper()


def word_count(text: str) -> int:
    """Return the number of words in text."""
    return len(text.split(" "))
