"""Text helpers shared by the reviews modules: ANSI stripping and byte caps."""

from __future__ import annotations

import re

# CSI sequences (colours, cursor moves), OSC sequences (titles, hyperlinks) and
# two-byte escapes. R27: logs are fetched raw and stripped before any cap.
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def as_text(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def keep_last_bytes(text: str, cap: int) -> str:
    """The longest suffix of text within cap bytes, starting on a line boundary when one fits."""
    data = text.encode("utf-8")
    if len(data) <= cap:
        return text
    tail = data[len(data) - cap:].decode("utf-8", errors="ignore")
    nl = tail.find("\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1:]
    return tail


def keep_first_bytes(text: str, cap: int) -> str:
    """The longest prefix of text within cap bytes."""
    data = text.encode("utf-8")
    if len(data) <= cap:
        return text
    return data[:cap].decode("utf-8", errors="ignore")


def collapse_ws(text: str) -> str:
    return " ".join(text.split())
