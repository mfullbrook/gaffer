"""Packets: what the writer sees after a round or a failed VERIFY.

Contents (R12, R13, R24, R27): admitted feedback items, failing tester/guard
tests, and failed required checks (title, summary, up to 50 annotations, the
last 200 log lines with ANSI stripped). Caps: 16 KB per check and 48 KB per
packet, both cut from the end: a check keeps its newest log lines, and the
packet keeps its first items, so the cap eats CI logs (placed last) before
review findings. Items cut off entirely are listed as omitted and stay
unprocessed.

R15: every item's text passes the secret scanner before it enters the packet;
a hit raises SecretDetected (BLOCKED: secret_detected). The gitleaks scanner is
T7's; this module needs only the SecretScanner interface.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ._text import keep_first_bytes, keep_last_bytes, utf8_len
from .checks import CHECK_CAP_BYTES, CheckEvidence
from .coderabbit import FeedbackItem, Source
from .junit import InventoryEntry

PACKET_CAP_BYTES = 48 * 1024
TEST_OUTPUT_CAP_BYTES = 4 * 1024  # per failing test (R24 "bounded"); not set by the ledger
_RESERVE = 512  # room for the omission note


@dataclass(frozen=True)
class SecretFinding:
    """A redacted finding: never the secret itself."""

    rule: str
    description: str = ""


class SecretScanner(Protocol):
    def scan(self, text: str) -> Sequence[SecretFinding]: ...


class SecretDetected(Exception):
    def __init__(self, item_key: tuple[str, str, str], findings: Sequence[SecretFinding]):
        self.item_key = item_key
        self.findings = tuple(findings)
        rules = ", ".join(sorted({f.rule for f in self.findings}))
        super().__init__(f"secret detected in packet item {item_key[0]}/{item_key[1]} ({rules})")


@dataclass(frozen=True)
class PacketItem:
    key: tuple[str, str, str]
    text: str
    truncated: bool = False


@dataclass(frozen=True)
class Packet:
    head_sha: str
    seq: int
    items: tuple[PacketItem, ...]
    omitted_keys: tuple[tuple[str, str, str], ...]
    text: str

    @property
    def included_keys(self) -> tuple[tuple[str, str, str], ...]:
        """Keys to mark processed once the resume result is recorded."""
        return tuple(i.key for i in self.items)

    @property
    def size(self) -> int:
        return utf8_len(self.text)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_feedback(item: FeedbackItem) -> str:
    where = ""
    if item.path:
        lines = ""
        if item.line_start is not None:
            lines = f":{item.line_start}" + (f"-{item.line_end}" if item.line_end not in (None, item.line_start) else "")
        where = f" {item.path}{lines}"
    head = f"### {item.source} {item.kind} {item.item_id}{where} (by {item.author})"
    parts = [head, "", item.body]
    for reply in item.context:
        parts += ["", "> thread reply (context):", reply]
    return "\n".join(parts).strip() + "\n"


def render_check(e: CheckEvidence, cap: int = CHECK_CAP_BYTES) -> str:
    """One failed check within `cap` bytes: the log tail is cut from its start
    (keeping the newest lines), then annotations from the end."""
    head = [f"### check {e.name}: {e.conclusion}"]
    if e.url:
        head.append(e.url)
    if e.title:
        head.append(f"Title: {e.title}")
    if e.summary:
        head.append(f"Summary: {e.summary}")
    notes = []
    if e.annotations:
        more = f" of {e.annotations_total}" if e.annotations_total > len(e.annotations) else ""
        notes.append(f"Annotations ({len(e.annotations)}{more}):")
        for a in e.annotations:
            loc = f"{a.get('path')}:{a.get('start_line')}"
            title = f" {a.get('title')}:" if a.get("title") else ""
            notes.append(f"- [{a.get('annotation_level')}] {loc}{title} {a.get('message')}".rstrip())
    header = "\n".join(head + notes) + "\n"
    if utf8_len(header) > cap // 2:
        header = keep_first_bytes(header, cap // 2) + "\n[annotations truncated]\n"
    if e.log_tail is None:
        return header + (f"(no log: {e.log_note})\n" if e.log_note else "")
    label = "Log tail (ANSI stripped):\n"
    room = cap - utf8_len(header) - utf8_len(label)
    return header + label + keep_last_bytes(e.log_tail, max(room, 0))


def render_failing_test(entry: InventoryEntry) -> str:
    t = entry.test
    case = entry.case or entry.collection_error
    out = case.text if case and case.text else entry.detail
    body = keep_first_bytes(out or "", TEST_OUTPUT_CAP_BYTES)
    return (
        f"### test {t.label.value} {t.criterion_id}: {t.file} :: {t.classname} :: {t.name}\n"
        f"Outcome: {entry.outcome.value}. {entry.detail}\n\n{body}\n"
    )


def build_packet(
    *,
    head_sha: str,
    seq: int,
    scanner: SecretScanner,
    items: Sequence[FeedbackItem] = (),
    failing_tests: Sequence[InventoryEntry] = (),
    failed_checks: Sequence[CheckEvidence] = (),
    cap: int = PACKET_CAP_BYTES,
) -> Packet:
    """Order: admitted items, failing tests, failed checks. Raises SecretDetected."""
    blocks: list[tuple[tuple[str, str, str], str]] = []
    for it in items:
        blocks.append((it.key, render_feedback(it)))
    for entry in failing_tests:
        text = render_failing_test(entry)
        blocks.append(((Source.TESTS.value, entry.test.identity, _hash(text)), text))
    for e in failed_checks:
        text = render_check(e)
        blocks.append(((Source.CI.value, str(e.check_run_id), _hash(text)), text))

    for key, text in blocks:
        findings = scanner.scan(text)
        if findings:
            raise SecretDetected(key, findings)

    header = f"# gaffer packet: head {head_sha[:12]}, round {seq}\n\n"
    used = utf8_len(header)
    included: list[PacketItem] = []
    omitted: list[tuple[str, str, str]] = []
    full = False
    for key, text in blocks:
        if full:
            omitted.append(key)
            continue
        size = utf8_len(text) + 1
        if used + size <= cap - _RESERVE:
            included.append(PacketItem(key, text))
            used += size
            continue
        full = True
        room = cap - _RESERVE - used - 64
        if room > 256:
            cut = keep_first_bytes(text, room) + "\n[truncated by the 48 KB packet cap]\n"
            included.append(PacketItem(key, cut, truncated=True))
            used += utf8_len(cut) + 1
        else:
            omitted.append(key)
    body = "\n".join(i.text for i in included)
    if omitted:
        body += f"\n[{len(omitted)} more item(s) omitted by the 48 KB packet cap; they stay unprocessed]\n"
    return Packet(head_sha, seq, tuple(included), tuple(omitted), header + body)
