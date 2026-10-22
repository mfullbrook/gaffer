"""CodeRabbit completion and feedback-item admission (R12 as amended by R27).

Completion for head H: a commit status on H with context "CodeRabbit", creator
coderabbitai[bot], state success and description exactly "Review completed".
"Review skipped…" or "Review paused", or no CodeRabbit status within
coderabbit_appear_timeout, completes the round with CodeRabbit unverified and
a reason. Any other description is logged verbatim and is not completion.

Admission (stateless; the round subtracts already-processed keys):
- CodeRabbit inline review comments: top-level only (no in_reply_to_id), on
  unresolved, non-outdated threads. Replies are context for their thread;
  outdated threads are context; resolved threads are ignored.
- CodeRabbit review bodies: only entries under "⚠️ Outside diff range
  comments" and "🧹 Nitpick comments", parsed by section (the "Actionable
  comments posted" header can be absent). An outside-diff entry on the same
  path with overlapping lines as an open CodeRabbit thread is a duplicate.
- Comments by operator_login, excluding any with a line matching `^gaffer-round: `.
- Everything else is ignored and listed: CodeRabbit summary/status comments
  (every version), comments by anyone else, PR-body edits.
- Codex review findings (R31): every finding of the Codex review recorded for
  the current head is an item with source "codex", its id prefixed with the
  head SHA; a review recorded for any other head is ignored.
- Codex "unjustified" harness judgements (R32, amending R7): each is an item
  with source "codex" too, so the writer reverts or justifies the change in the
  next resume (a repair cycle). "justified" judgements stay report-only.
Items are keyed by (source, item id, content hash); the hash is over the body
with NORMALIZE_PATTERN lines removed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

CODERABBIT_LOGIN = "coderabbitai[bot]"
STATUS_CONTEXT = "CodeRabbit"
REVIEW_COMPLETED = "Review completed"
NEVER_STARTED = "CodeRabbit never started on this head; the repo may not be covered by the app"

# R27: lives with the resolution_marker_edit fixture (expected.json "normalization").
NORMALIZE_PATTERN = (
    r"^(✅ Addressed in commits? .*|✅ Confirmed as addressed by @\S+.*"
    r"|<!-- This is an auto-generated (comment|reply) by CodeRabbit -->)\s*$"
)
_NORMALIZE = re.compile(NORMALIZE_PATTERN)

ROUND_MARKER = re.compile(r"^gaffer-round: ", re.M)  # R9 marker on gaffer's own round comment
RELEASE_NOTES_START = "<!-- This is an auto-generated comment: release notes by coderabbit.ai -->"
RELEASE_NOTES_END = "<!-- end of auto-generated comment: release notes by coderabbit.ai -->"
SUMMARY_MARKER = "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->"


class Source(StrEnum):
    """Item sources. The values are the store's ITEM_SOURCES (T6), so a key goes
    straight into Store.add_item(source=key[0], item_id=key[1], content_hash=key[2])."""

    CODERABBIT = "coderabbit"
    OPERATOR = "operator"
    CI = "check"
    TESTS = "test"
    CODEX = "codex"


class ItemKind(StrEnum):
    INLINE_COMMENT = "inline_comment"
    OUTSIDE_DIFF = "outside_diff"
    NITPICK = "nitpick"
    ISSUE_COMMENT = "issue_comment"
    REVIEW_COMMENT = "review_comment"
    REVIEW_BODY = "review_body"
    CODEX_FINDING = "codex_finding"
    HARNESS_JUDGEMENT = "harness_judgement"  # R32: a Codex "unjustified" verdict on a tester/harness change


# --- normalization and identity -------------------------------------------------


def normalize_body(body: str) -> str:
    """Drop status/resolution marker lines (R27), trailing spaces and repeated blank lines."""
    out: list[str] = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if _NORMALIZE.match(line):
            continue
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def content_hash(body: str) -> str:
    return hashlib.sha256(normalize_body(body).encode("utf-8")).hexdigest()


def login_of(user: Mapping[str, Any] | None) -> str:
    """REST says coderabbitai[bot], GraphQL says coderabbitai: compare without [bot]."""
    return str((user or {}).get("login") or "")


def _same_login(a: str, b: str) -> bool:
    return a.lower().removesuffix("[bot]") == b.lower().removesuffix("[bot]")


def is_coderabbit(login: str) -> bool:
    return _same_login(login, CODERABBIT_LOGIN)


def has_round_marker(body: str) -> bool:
    return bool(ROUND_MARKER.search(body or ""))


def pr_body_outside_release_notes(body: str) -> str:
    """R27: readers of the PR body (the gaffer-job marker, R23) see only text
    outside CodeRabbit's release-notes markers."""
    text = body or ""
    while True:
        start = text.find(RELEASE_NOTES_START)
        if start < 0:
            return text.strip()
        end = text.find(RELEASE_NOTES_END, start)
        if end < 0:
            return text[:start].strip()
        text = text[:start] + text[end + len(RELEASE_NOTES_END):]


# --- completion signal (R27) -----------------------------------------------------


class CodeRabbitState(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class CodeRabbitStatus:
    state: CodeRabbitState
    reason: str = ""
    description: str | None = None
    unrecognized: bool = False  # log the description verbatim

    @property
    def done(self) -> bool:
        return self.state is not CodeRabbitState.PENDING

    @property
    def verified(self) -> bool:
        return self.state is CodeRabbitState.COMPLETE


def coderabbit_status(
    statuses: Iterable[Mapping[str, Any]],
    *,
    seconds_since_push: float,
    appear_timeout: float,
    check_suite_present: bool | None = None,
) -> CodeRabbitStatus:
    """Classify the head's CodeRabbit status from GET /commits/{H}/statuses.

    Only the newest status with context "CodeRabbit" created by
    coderabbitai[bot] counts; a status from any other creator is ignored.
    """
    mine = [
        s
        for s in statuses
        if s.get("context") == STATUS_CONTEXT and login_of(s.get("creator")) == CODERABBIT_LOGIN
    ]
    if not mine:
        if seconds_since_push >= appear_timeout:
            reason = NEVER_STARTED
            if check_suite_present is False:
                reason += " (no coderabbitai check suite on the head)"
            return CodeRabbitStatus(CodeRabbitState.UNVERIFIED, reason)
        return CodeRabbitStatus(CodeRabbitState.PENDING, "no CodeRabbit status yet")
    latest = max(mine, key=lambda s: (str(s.get("created_at") or ""), int(s.get("id") or 0)))
    state, desc = latest.get("state"), str(latest.get("description") or "")
    if state == "success" and desc == REVIEW_COMPLETED:
        return CodeRabbitStatus(CodeRabbitState.COMPLETE, "Review completed", desc)
    if desc.startswith("Review skipped") or desc == "Review paused":
        return CodeRabbitStatus(CodeRabbitState.UNVERIFIED, f"CodeRabbit will not review this head: status {desc!r}", desc)
    if state == "pending" or desc == "Review approved":
        return CodeRabbitStatus(CodeRabbitState.PENDING, f"status {state} {desc!r}", desc)
    return CodeRabbitStatus(
        CodeRabbitState.PENDING, f"unrecognized CodeRabbit status {state!r} {desc!r}; not treated as complete", desc, unrecognized=True
    )


# --- items ------------------------------------------------------------------------


@dataclass(frozen=True)
class FeedbackItem:
    source: Source
    item_id: str
    content_hash: str
    kind: ItemKind | str
    author: str
    body: str  # normalized
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    url: str = ""
    context: tuple[str, ...] = ()  # CodeRabbit replies in the same thread (R27)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source.value if isinstance(self.source, Source) else str(self.source), self.item_id, self.content_hash)


@dataclass(frozen=True)
class Disregarded:
    """A comment that is not an item: context (outdated threads, CodeRabbit
    thread replies) or ignored (listed in the report)."""

    source: str
    item_id: str
    kind: str
    author: str
    reason: str


@dataclass(frozen=True)
class Admission:
    admitted: tuple[FeedbackItem, ...]
    context: tuple[Disregarded, ...] = ()
    ignored: tuple[Disregarded, ...] = ()
    not_reviewed_files: tuple[str, ...] = ()  # "Files not reviewed due to moderation or processing errors"

    @property
    def keys(self) -> frozenset[tuple[str, str, str]]:
        return frozenset(i.key for i in self.admitted)

    def verdict(self, item_id: str | int) -> str | None:
        """"admit", "context" or "ignore" for an item or comment id (as in the fixtures' expected.json)."""
        item_id = str(item_id)
        if any(i.item_id == item_id for i in self.admitted):
            return "admit"
        if any(d.item_id == item_id for d in self.context):
            return "context"
        if any(d.item_id == item_id for d in self.ignored):
            return "ignore"
        return None


class IncompleteData(Exception):
    """The snapshot contradicts itself (e.g. an inline comment with no review
    thread). Treated like an API error: the poll proves nothing (design)."""


def admit(
    *,
    head_sha: str,
    operator_login: str,
    reviews: Sequence[Mapping[str, Any]] = (),
    review_comments: Sequence[Mapping[str, Any]] = (),
    issue_comments: Sequence[Mapping[str, Any]] = (),
    review_threads: Sequence[Mapping[str, Any]] = (),
    codex_reviews: Sequence[CodexReview] = (),
) -> Admission:
    admitted: list[FeedbackItem] = []
    context: list[Disregarded] = []
    ignored: list[Disregarded] = []
    not_reviewed: list[str] = []

    threads = _index_threads(review_threads)
    replies: dict[int, list[str]] = {}

    for c in review_comments:
        cid = int(c["id"])
        author = login_of(c.get("user"))
        body = str(c.get("body") or "")
        parent = c.get("in_reply_to_id")
        kind = ItemKind.REVIEW_COMMENT
        if is_coderabbit(author):
            if parent:
                replies.setdefault(int(parent), []).append(normalize_body(body))
                context.append(Disregarded("coderabbit", str(cid), "thread_reply", author, "CodeRabbit reply inside a thread: context for its thread (R27)"))
                continue
            source, kind = Source.CODERABBIT, ItemKind.INLINE_COMMENT
        elif _same_login(author, operator_login):
            if has_round_marker(body):
                ignored.append(Disregarded("operator", str(cid), "review_comment", author, "gaffer's own comment (gaffer-round: line, R27)"))
                continue
            source = Source.OPERATOR
        else:
            ignored.append(Disregarded("other", str(cid), "review_comment", author, f"comment by someone else ({author})"))
            continue
        thread = threads.get(int(parent) if parent else cid) or threads.get(cid)
        if thread is None:
            raise IncompleteData(f"review comment {cid} has no review thread in the GraphQL snapshot")
        if thread.get("isResolved"):
            by = login_of(thread.get("resolvedBy"))
            ignored.append(Disregarded(source.value, str(cid), kind.value, author, f"resolved thread (resolved by {by or 'unknown'})"))
            continue
        if thread.get("isOutdated"):
            context.append(Disregarded(source.value, str(cid), kind.value, author, "outdated thread: context only"))
            continue
        line_end = c.get("line") or thread.get("line")
        line_start = c.get("start_line") or thread.get("startLine") or line_end
        admitted.append(
            FeedbackItem(
                source=source,
                item_id=str(cid),
                content_hash=content_hash(body),
                kind=kind,
                author=author,
                body=normalize_body(body),
                path=c.get("path"),
                line_start=line_start,
                line_end=line_end,
                url=str(c.get("html_url") or ""),
            )
        )

    # Attach CodeRabbit thread replies to their root item as context.
    admitted = [
        FeedbackItem(**{**i.__dict__, "context": tuple(replies.get(int(i.item_id), ()))})
        if i.kind is ItemKind.INLINE_COMMENT and replies.get(int(i.item_id))
        else i
        for i in admitted
    ]

    open_cr_threads = _open_coderabbit_threads(review_threads)
    for r in reviews:
        rid = str(r["id"])
        author = login_of(r.get("user"))
        body = str(r.get("body") or "")
        if not body.strip():
            continue
        if is_coderabbit(author):
            if r.get("commit_id") and r.get("commit_id") != head_sha:
                ignored.append(Disregarded("coderabbit", rid, "review_body", author, "review body for an earlier head"))
                continue
            parsed = parse_review_body(body)
            not_reviewed.extend(parsed.not_reviewed_files)
            if not parsed.entries:
                ignored.append(Disregarded("coderabbit", rid, "review_body", author, "review body with no actionable section"))
                continue
            for e in parsed.entries:
                item_id = f"{rid}/{e.kind.value}/{e.index}"
                dup = _duplicate_thread(e, open_cr_threads) if e.kind is ItemKind.OUTSIDE_DIFF else None
                if dup is not None:
                    ignored.append(Disregarded("coderabbit", item_id, e.kind.value, author, f"duplicate of open CodeRabbit thread on {e.path} (comment {dup})"))
                    continue
                admitted.append(
                    FeedbackItem(
                        source=Source.CODERABBIT,
                        item_id=item_id,
                        content_hash=content_hash(e.text),
                        kind=e.kind,
                        author=author,
                        body=normalize_body(e.text),
                        path=e.path,
                        line_start=e.line_start,
                        line_end=e.line_end,
                        url=str(r.get("html_url") or ""),
                    )
                )
        elif _same_login(author, operator_login):
            if has_round_marker(body):
                ignored.append(Disregarded("operator", rid, "review_body", author, "gaffer's own comment (gaffer-round: line, R27)"))
                continue
            admitted.append(
                FeedbackItem(Source.OPERATOR, rid, content_hash(body), ItemKind.REVIEW_BODY, author, normalize_body(body), url=str(r.get("html_url") or ""))
            )
        else:
            ignored.append(Disregarded("other", rid, "review_body", author, f"review by someone else ({author})"))

    for c in issue_comments:
        cid = str(c["id"])
        author = login_of(c.get("user"))
        body = str(c.get("body") or "")
        if is_coderabbit(author):
            what = "walkthrough/summary" if SUMMARY_MARKER in body else "status"
            ignored.append(Disregarded("coderabbit", cid, "issue_comment", author, f"CodeRabbit {what} comment; every version ignored (R12)"))
        elif _same_login(author, operator_login):
            if has_round_marker(body):
                ignored.append(Disregarded("operator", cid, "issue_comment", author, "gaffer's own round comment (gaffer-round: line, R27)"))
                continue
            admitted.append(
                FeedbackItem(Source.OPERATOR, cid, content_hash(body), ItemKind.ISSUE_COMMENT, author, normalize_body(body), url=str(c.get("html_url") or ""))
            )
        else:
            ignored.append(Disregarded("other", cid, "issue_comment", author, f"comment by someone else ({author})"))

    for review in codex_reviews:
        if review.head_sha != head_sha:
            ignored.append(Disregarded("codex", review.head_sha, "codex_review", CODEX_AUTHOR, "Codex review for an earlier head"))
            continue
        admitted.extend(codex_items(review))

    return Admission(tuple(admitted), tuple(context), tuple(ignored), tuple(dict.fromkeys(not_reviewed)))


# --- Codex review findings (R31) -------------------------------------------------------

CODEX_AUTHOR = "codex"


@dataclass(frozen=True)
class CodexReview:
    """The Codex review recorded for one head (once per head, design "Codex
    reviewer"). findings are children.ReviewFinding objects or mappings with
    id, severity, path, line, title and body; harness_judgements are
    children.HarnessJudgement objects or mappings with path, verdict and reason
    (R7, R32)."""

    head_sha: str
    findings: tuple[Any, ...] = ()
    harness_judgements: tuple[Any, ...] = ()


def _field(f: Any, name: str) -> Any:
    return f.get(name) if isinstance(f, Mapping) else getattr(f, name, None)


def codex_item_id(head_sha: str, finding_id: str) -> str:
    """Head SHA first, so the same finding text on a new head is a new item (R31, T6 report item 2)."""
    return f"{head_sha}/{finding_id}"


def harness_item_id(head_sha: str, path: str) -> str:
    """One item per judged path per head (R32)."""
    return f"{head_sha}/harness/{path}"


UNJUSTIFIED = "unjustified"


def codex_items(review: CodexReview) -> tuple[FeedbackItem, ...]:
    """Packet items from a Codex review: every finding (R31) and every
    "unjustified" harness judgement (R32)."""
    out: dict[tuple[str, str, str], FeedbackItem] = {}
    for f in review.findings:
        fid = str(_field(f, "id") or "").strip()
        if not fid:
            raise ValueError("a Codex finding has no id")
        severity = str(_field(f, "severity") or "")
        title = str(_field(f, "title") or "")
        body = str(_field(f, "body") or "")
        text = f"[{severity}] {title}\n\n{body}".strip() if severity else f"{title}\n\n{body}".strip()
        line = _field(f, "line")
        line = line if isinstance(line, int) and not isinstance(line, bool) else None
        path = _field(f, "path")
        item = FeedbackItem(
            source=Source.CODEX,
            item_id=codex_item_id(review.head_sha, fid),
            content_hash=content_hash(text),
            kind=ItemKind.CODEX_FINDING,
            author=CODEX_AUTHOR,
            body=normalize_body(text),
            path=path if isinstance(path, str) and path else None,
            line_start=line,
            line_end=line,
        )
        out.setdefault(item.key, item)
    for j in review.harness_judgements:
        if str(_field(j, "verdict") or "") != UNJUSTIFIED:
            continue
        path = str(_field(j, "path") or "").strip()
        if not path:
            raise ValueError("a Codex harness judgement has no path")
        reason = str(_field(j, "reason") or "").strip() or "(no reason given)"
        text = (f"[unjustified harness change] {path}\n\n"
                f"The Codex reviewer judged your change to this tester, harness or CI file unjustified (R7): {reason}\n\n"
                f"Revert the change in a new commit (disposition fixed), or decline this item with the reason the "
                f"change is needed.")
        item = FeedbackItem(
            source=Source.CODEX,
            item_id=harness_item_id(review.head_sha, path),
            content_hash=content_hash(text),
            kind=ItemKind.HARNESS_JUDGEMENT,
            author=CODEX_AUTHOR,
            body=normalize_body(text),
            path=path,
        )
        out.setdefault(item.key, item)
    return tuple(out.values())


def _index_threads(threads: Iterable[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    out: dict[int, Mapping[str, Any]] = {}
    for t in threads:
        for n in ((t.get("comments") or {}).get("nodes") or ()):
            if n.get("databaseId") is not None:
                out[int(n["databaseId"])] = t
    return out


@dataclass(frozen=True)
class _OpenThread:
    path: str
    start: int
    end: int
    root_id: int | None


def _open_coderabbit_threads(threads: Iterable[Mapping[str, Any]]) -> list[_OpenThread]:
    out = []
    for t in threads:
        if t.get("isResolved") or t.get("isOutdated") or not t.get("line"):
            continue
        nodes = (t.get("comments") or {}).get("nodes") or ()
        if not nodes or not is_coderabbit(login_of(nodes[0].get("author"))):
            continue
        end = int(t["line"])
        start = int(t.get("startLine") or end)
        out.append(_OpenThread(str(t.get("path") or ""), min(start, end), max(start, end), nodes[0].get("databaseId")))
    return out


def _duplicate_thread(entry: BodyEntry, threads: Sequence[_OpenThread]) -> int | str | None:
    if not entry.path or entry.line_start is None:
        return None
    end = entry.line_end if entry.line_end is not None else entry.line_start
    for t in threads:
        if _same_path(t.path, entry.path) and entry.line_start <= t.end and t.start <= end:
            return t.root_id if t.root_id is not None else t.path
    return None


def _same_path(a: str, b: str) -> bool:
    a, b = a.strip("/"), b.strip("/")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


# --- review-body sections ------------------------------------------------------------


SECTION_KINDS = {"Outside diff range comments": ItemKind.OUTSIDE_DIFF, "Nitpick comments": ItemKind.NITPICK}
_SECTION = re.compile(r"(?:⚠️\s*)?(Outside diff range comments|(?:🧹\s*)?Nitpick comments)\s*\((\d+)\)")
_NOT_REVIEWED = re.compile(r"Files not reviewed due to moderation or processing errors")
_TAG = re.compile(r"<details>|</details>|<summary>(.*?)</summary>", re.S)
_LOC_LINE = re.compile(r"^`([^`\s]+?):(\d+)(?:-(\d+))?`\s*$")
_LOC_CODE = re.compile(r"<code>([^<\s]+?):(\d+)(?:-(\d+))?</code>")
_CLASSIC_ENTRY = re.compile(r"^`(\d+)(?:-(\d+))?`:\s", re.M)
_FILE_SUMMARY = re.compile(r"^(.+?)\s+\((\d+)\)$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HTML_TAG = re.compile(r"</?(?:blockquote|strong|code|b|i|em|sub|sup|br)\s*/?>")


@dataclass(frozen=True)
class BodyEntry:
    kind: ItemKind
    index: int
    text: str
    path: str | None
    line_start: int | None
    line_end: int | None


@dataclass(frozen=True)
class ParsedBody:
    entries: tuple[BodyEntry, ...]
    not_reviewed_files: tuple[str, ...] = ()


@dataclass
class _Node:
    summary: str | None = None
    parts: list[str | _Node] = field(default_factory=list)


def parse_review_body(body: str) -> ParsedBody:
    """Entries under the actionable sections of a CodeRabbit review body.

    Two layouts are handled: October 2026 (fixtures): a bold section header in a
    `> [!CAUTION]` block followed by one <details> per entry whose first line is
    `path:lines`; and the earlier layout (Phase 1, synthetic test only): a
    <details> section whose children are per-file <details> holding
    "`lines`: **title**" entries.
    """
    tree = _details_tree(_unquote(body))
    entries: list[BodyEntry] = []
    counters: dict[ItemKind, int] = {}
    not_reviewed: list[str] = []

    def add(kind: ItemKind, text: str, path: str | None, start: int | None, end: int | None) -> None:
        counters[kind] = counters.get(kind, 0) + 1
        entries.append(BodyEntry(kind, counters[kind], text.strip(), path, start, end))

    def walk(node: _Node) -> None:
        parts = node.parts
        i = 0
        while i < len(parts):
            p = parts[i]
            if isinstance(p, str):
                m = _SECTION.search(p)
                if m and not _NOT_REVIEWED.search(p):
                    kind = _kind(m.group(1))
                    j = i + 1
                    while j < len(parts):
                        q = parts[j]
                        if isinstance(q, str):
                            if q.strip():
                                break
                        else:
                            _emit_entry(q, kind, add)
                        j += 1
                    i = j
                    continue
                if _NOT_REVIEWED.search(p):
                    not_reviewed.extend(_bullets(p.split(_NOT_REVIEWED.search(p).group(0), 1)[1]) or ["(files not parsed)"])
            else:
                summary = p.summary or ""
                m = _SECTION.search(summary)
                if m:
                    kind = _kind(m.group(1))
                    for q in p.parts:
                        if isinstance(q, _Node):
                            fm = _FILE_SUMMARY.match(_plain(q.summary or ""))
                            if fm and _CLASSIC_ENTRY.search(_render_parts(q.parts)):
                                for path, start, end, text in _classic_entries(fm.group(1), q):
                                    add(kind, text, path, start, end)
                            else:
                                _emit_entry(q, kind, add)
                elif _NOT_REVIEWED.search(summary):
                    not_reviewed.extend(_bullets(_render_parts(p.parts)) or ["(files not parsed)"])
                else:
                    walk(p)
            i += 1

    walk(tree)
    return ParsedBody(tuple(entries), tuple(dict.fromkeys(not_reviewed)))


def _kind(label: str) -> ItemKind:
    return ItemKind.OUTSIDE_DIFF if "Outside diff" in label else ItemKind.NITPICK


def _emit_entry(node: _Node, kind: ItemKind, add) -> None:
    content = _render_parts(node.parts)
    path = start = end = None
    for line in content.splitlines():
        if line.strip():
            m = _LOC_LINE.match(line.strip())
            if m:
                path, start, end = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
            break
    if path is None:
        m = _LOC_CODE.search(node.summary or "")
        if m:
            path, start, end = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
    add(kind, f"{_plain(node.summary or '')}\n\n{content}", path, start, end)


def _classic_entries(path: str, node: _Node):
    text = _render_parts(node.parts)
    starts = list(_CLASSIC_ENTRY.finditer(text))
    for k, m in enumerate(starts):
        chunk = text[m.start(): starts[k + 1].start() if k + 1 < len(starts) else len(text)]
        chunk = re.sub(r"\n-{3,}\s*$", "", chunk.strip())
        start = int(m.group(1))
        yield path, start, int(m.group(2) or start), f"{path}\n\n{chunk}"


def _unquote(body: str) -> str:
    return "\n".join(re.sub(r"^(?:>[ \t]?)+", "", ln) for ln in (body or "").replace("\r\n", "\n").split("\n"))


def _details_tree(text: str) -> _Node:
    root = _Node()
    stack = [root]
    pos = 0
    for m in _TAG.finditer(text):
        stack[-1].parts.append(text[pos:m.start()])
        tok = m.group(0)
        if tok == "<details>":
            node = _Node()
            stack[-1].parts.append(node)
            stack.append(node)
        elif tok == "</details>":
            if len(stack) > 1:
                stack.pop()
        else:
            if stack[-1].summary is None and len(stack) > 1:
                stack[-1].summary = m.group(1)
            else:
                stack[-1].parts.append(m.group(1))
        pos = m.end()
    stack[-1].parts.append(text[pos:])
    return root


def _plain(s: str) -> str:
    return _HTML_TAG.sub("", _HTML_COMMENT.sub("", s)).strip()


def _render_parts(parts: Sequence[str | _Node]) -> str:
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(_plain(p) if p.strip() else p)
        else:
            out.append(f"\n{_plain(p.summary or '')}\n{_render_parts(p.parts)}\n")
    text = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _bullets(text: str) -> list[str]:
    files = []
    for line in _plain(text).splitlines():
        m = re.match(r"^\s*[*-]\s+`?([^`\s]+)`?", line)
        if m:
            files.append(m.group(1))
    return files
