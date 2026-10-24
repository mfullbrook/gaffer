# Hidden acceptance checks for EVAL-2 (see EVAL-1/hidden_test.py).
from toy.textutil import initials, shout, word_count


def test_two_words():
    assert initials("ada lovelace") == "AL"


def test_extra_whitespace():
    assert initials("  grace   brewster hopper ") == "GBH"


def test_empty():
    assert initials("") == ""


def test_first_letter_skips_punctuation_feedback():
    assert initials("ada (byron) lovelace") == "ABL"
    assert initials('"grace" hopper') == "GH"


def test_word_without_letters_adds_nothing_feedback():
    assert initials("ada & grace") == "AG"


def test_existing_helpers_unchanged():
    assert shout("hi") == "HI"
    assert word_count("one two three") == 3
