# Hidden acceptance checks for EVAL-2 (see EVAL-1/hidden_test.py).
from toy.textutil import initials, shout, word_count


def test_two_words():
    assert initials("ada lovelace") == "AL"


def test_extra_whitespace():
    assert initials("  grace   brewster hopper ") == "GBH"


def test_empty():
    assert initials("") == ""


def test_hyphenated_feedback():
    assert initials("jean-paul sartre") == "JPS"


def test_existing_helpers_unchanged():
    assert shout("hi") == "HI"
    assert word_count("one two three") == 3
