"""Required-check evaluation (R11) and CI evidence for failed checks (R13 as amended by R27)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._text import as_text, keep_last_bytes, strip_ansi
from .github import GitHubError, GitHubReader

# R11 conclusion mapping.
PASS_CONCLUSIONS = frozenset({"success"})
SKIPPABLE_CONCLUSIONS = frozenset({"skipped", "neutral"})
FAIL_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required"})

# R13: per failed check.
MAX_ANNOTATIONS = 50
LOG_TAIL_LINES = 200
CHECK_CAP_BYTES = 16 * 1024

NO_CI_EVIDENCE = "NO CI EVIDENCE"


class CheckState(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"  # not yet concluded, or not yet appeared within check_appear_timeout
    MISSING = "missing"  # absent check_appear_timeout after push: BLOCKED: required_check_missing


@dataclass(frozen=True)
class CheckVerdict:
    name: str
    state: CheckState
    reason: str
    conclusion: str | None = None
    check_run: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RequiredChecks:
    verdicts: tuple[CheckVerdict, ...]
    no_ci_evidence: bool = False  # allow_no_ci with an empty list: stamp every READY report

    def _with(self, state: CheckState) -> tuple[CheckVerdict, ...]:
        return tuple(v for v in self.verdicts if v.state is state)

    @property
    def failed(self) -> tuple[CheckVerdict, ...]:
        return self._with(CheckState.FAIL)

    @property
    def missing(self) -> tuple[CheckVerdict, ...]:
        return self._with(CheckState.MISSING)

    @property
    def pending(self) -> tuple[CheckVerdict, ...]:
        return self._with(CheckState.PENDING)

    @property
    def concluded(self) -> bool:
        """Every required check passed or failed (R11); a missing one is never concluded."""
        return all(v.state in (CheckState.PASS, CheckState.FAIL) for v in self.verdicts)

    @property
    def all_passed(self) -> bool:
        return all(v.state is CheckState.PASS for v in self.verdicts)


def evaluate_required_checks(
    required: Sequence[str],
    skippable: Iterable[str],
    check_runs: Iterable[Mapping[str, Any]],
    *,
    statuses: Iterable[Mapping[str, Any]] = (),
    seconds_since_push: float,
    appear_timeout: float,
) -> RequiredChecks:
    """Evaluate config.required_checks on one head.

    `check_runs` is GET /commits/{sha}/check-runs (`check_runs` list); a name
    seen several times uses its latest run. `statuses` (commit statuses) are
    also matched by context, for CI that reports statuses rather than check
    runs. `seconds_since_push` is the round's supervised time (R25).
    """
    if not required:
        return RequiredChecks((), no_ci_evidence=True)
    skippable = set(skippable)
    runs = _latest_by(check_runs, "name", ("started_at", "id"))
    stats = _latest_by(statuses, "context", ("created_at", "id"))
    verdicts = []
    for name in required:
        if name in runs:
            verdicts.append(_run_verdict(name, runs[name], name in skippable))
        elif name in stats:
            verdicts.append(_status_verdict(name, stats[name]))
        elif seconds_since_push >= appear_timeout:
            verdicts.append(CheckVerdict(name, CheckState.MISSING, f"no check named {name!r} on the head {int(appear_timeout)} s after push (R11)"))
        else:
            verdicts.append(CheckVerdict(name, CheckState.PENDING, "not reported yet"))
    return RequiredChecks(tuple(verdicts))


def _run_verdict(name: str, run: Mapping[str, Any], skippable: bool) -> CheckVerdict:
    if run.get("status") != "completed":
        return CheckVerdict(name, CheckState.PENDING, f"status {run.get('status')!r}", None, run)
    c = run.get("conclusion")
    if c in PASS_CONCLUSIONS:
        return CheckVerdict(name, CheckState.PASS, "success", c, run)
    if c in SKIPPABLE_CONCLUSIONS:
        if skippable:
            return CheckVerdict(name, CheckState.PASS, f"{c}, listed in skippable_checks", c, run)
        return CheckVerdict(name, CheckState.FAIL, f"{c}, not listed in skippable_checks (R11)", c, run)
    if c in FAIL_CONCLUSIONS:
        return CheckVerdict(name, CheckState.FAIL, str(c), c, run)
    # stale, startup_failure or anything new: fail closed.
    return CheckVerdict(name, CheckState.FAIL, f"unrecognized conclusion {c!r}, treated as failure", c, run)


def _status_verdict(name: str, status: Mapping[str, Any]) -> CheckVerdict:
    state = status.get("state")
    if state == "success":
        return CheckVerdict(name, CheckState.PASS, "status success", state)
    if state in ("failure", "error"):
        return CheckVerdict(name, CheckState.FAIL, f"status {state}: {status.get('description') or ''}".strip(), state)
    return CheckVerdict(name, CheckState.PENDING, f"status {state!r}", state)


def _latest_by(items: Iterable[Mapping[str, Any]], key: str, order: tuple[str, str]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for it in items:
        k = it.get(key)
        if not k:
            continue
        prev = out.get(k)
        if prev is None or _sort_key(it, order) > _sort_key(prev, order):
            out[k] = it
    return out


def _sort_key(it: Mapping[str, Any], order: tuple[str, str]) -> tuple[str, int]:
    return (str(it.get(order[0]) or ""), int(it.get(order[1]) or 0))


# --- CI evidence (R13, R27) ---------------------------------------------------------


@dataclass(frozen=True)
class CheckEvidence:
    name: str
    check_run_id: int
    conclusion: str | None
    title: str | None
    summary: str | None
    annotations: tuple[Mapping[str, Any], ...]
    annotations_total: int
    log_tail: str | None  # ANSI stripped, last 200 lines, capped from the end
    log_note: str = ""  # why there is no log tail
    url: str = ""


def log_tail(raw: bytes | str, *, lines: int = LOG_TAIL_LINES, cap: int = CHECK_CAP_BYTES) -> str:
    """R27: strip ANSI codes, then take the last `lines` lines, then cap from the end."""
    text = strip_ansi(as_text(raw))
    tail = "".join(text.splitlines(keepends=True)[-lines:])
    return keep_last_bytes(tail, cap)


def is_github_actions(check_run: Mapping[str, Any]) -> bool:
    app = check_run.get("app") or {}
    return app.get("slug") == "github-actions"


def collect_check_evidence(gh: GitHubReader, repo: str, check_run: Mapping[str, Any]) -> CheckEvidence:
    """Title, summary, up to 50 annotations and, for GitHub Actions, the job
    log tail, fetched with gaffer's own token (R13). A failed log fetch is
    recorded as a note, never as an empty log."""
    run_id = int(check_run["id"])
    output = check_run.get("output") or {}
    annotations: list[Mapping[str, Any]] = []
    total = int(output.get("annotations_count") or 0)
    if total:
        annotations = list(gh.check_run_annotations(repo, run_id))
        total = max(total, len(annotations))
    tail, note = None, ""
    if is_github_actions(check_run):
        try:
            tail = log_tail(gh.job_log(repo, run_id))
        except GitHubError as e:
            note = f"job log unavailable: {e}"
    else:
        note = "not a GitHub Actions check; no log fetched"
    return CheckEvidence(
        name=str(check_run.get("name") or ""),
        check_run_id=run_id,
        conclusion=check_run.get("conclusion"),
        title=output.get("title"),
        summary=output.get("summary"),
        annotations=tuple(annotations[:MAX_ANNOTATIONS]),
        annotations_total=total,
        log_tail=tail,
        log_note=note,
        url=str(check_run.get("html_url") or ""),
    )
