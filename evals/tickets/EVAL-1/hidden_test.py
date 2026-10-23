# Hidden acceptance checks for EVAL-1: never shown to the tester or writer. gaffer eval copies
# this file into a verification worktree of the final head and runs it with the toy's tests.
from toy.textutil import shout, word_count


def test_repeated_spaces():
    assert word_count("a  b") == 2


def test_leading_and_trailing():
    assert word_count("  leading and trailing  ") == 3


def test_empty_and_blank():
    assert word_count("") == 0
    assert word_count("   ") == 0


def test_single_spaces_unchanged():
    assert word_count("one two three") == 3


def test_tabs_and_newlines_feedback():
    assert word_count("a\tb\nc") == 3


def test_shout_unchanged():
    assert shout("hi") == "HI"
