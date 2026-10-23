# Hidden acceptance checks for EVAL-3 (see EVAL-1/hidden_test.py).
import pytest

from toy.sizes import human_size


def test_bytes():
    assert human_size(0) == "0 Bytes"
    assert human_size(1) == "1 Byte"


def test_kilobytes():
    assert human_size(1500) == "1.5 kB"


def test_megabytes():
    assert human_size(1_000_000) == "1.0 MB"


def test_uses_humanize():
    import humanize

    assert human_size(123456789) == humanize.naturalsize(123456789)


def test_negative_feedback():
    with pytest.raises(ValueError):
        human_size(-1500)
