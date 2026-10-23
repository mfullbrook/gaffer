from toy.textutil import shout, word_count


def test_shout_uppercases():
    assert shout("hi there") == "HI THERE"


def test_word_count_single_spaces():
    assert word_count("one two three") == 3
