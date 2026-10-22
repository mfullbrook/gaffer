"""Review rounds: completion, settle, late items and supervised-time deadlines.

A round is (repo, PR, head SHA, seq) and completes when (design "Rounds and
packets", R11, R27):
- every required check has concluded (pass or fail),
- the Codex review for the head is recorded,
- CodeRabbit is done for the head: "Review completed" (verified), or
  skipped/paused/never-started (unverified, with a reason), and
- 60 s of supervised time have passed since CodeRabbit was done and since
  the last new admitted item (the settle resets on each new item).
A configured check absent check_appear_timeout after push ends the round as
CHECK_MISSING (BLOCKED: required_check_missing). A round not complete after
round_timeout supervised seconds is TIMED_OUT (BLOCKED: review_timeout).

R25: deadlines use supervised seconds, accumulated from monotonic deltas inside
one gaffer process only. A restart starts a new process-local baseline, so time
while gaffer is stopped, crashed or the Mac sleeps is not counted. Wall/monotonic
gaps over 2 min are recorded (R17) for the report.

Items admitted after a round completed, and not in its packet, open round
seq+1 on the same head (design), whose timer starts at that first late item (R8).
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from ..config import RepoConfig
from .checks import RequiredChecks, evaluate_required_checks
from .coderabbit import Admission, CodeRabbitState, CodeRabbitStatus, admit, coderabbit_status
from .github import GitHubReader

SETTLE_SECONDS = 60
SLEEP_GAP_SECONDS = 120  # R17: recorded in the job log and report

Key = tuple[str, str, str]


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def wall(self) -> float: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()


class RoundStatus(StrEnum):
    WAITING = "waiting"
    COMPLETE = "complete"
    TIMED_OUT = "review_timeout"
    CHECK_MISSING = "required_check_missing"


@dataclass
class RoundState:
    """What T6's rounds row persists (supervised_seconds per R25)."""

    head_sha: str
    seq: int = 1
    supervised_seconds: float = 0.0
    since_push_at_open: float = 0.0  # supervised seconds since the push when this round opened (0 for seq 1)
    wall_opened_at: float | None = None
    seen_keys: set[Key] = field(default_factory=set)
    last_item_at: float | None = None  # supervised second of the newest admitted item
    coderabbit_done_at: float | None = None
    coderabbit_state: str = CodeRabbitState.PENDING.value
    coderabbit_reason: str = ""
    status: str = RoundStatus.WAITING.value
    packet_keys: set[Key] = field(default_factory=set)
    sleep_gaps: list[tuple[float, float]] = field(default_factory=list)  # (wall time, gap seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "head_sha": self.head_sha,
            "seq": self.seq,
            "supervised_seconds": self.supervised_seconds,
            "since_push_at_open": self.since_push_at_open,
            "wall_opened_at": self.wall_opened_at,
            "seen_keys": sorted(list(k) for k in self.seen_keys),
            "last_item_at": self.last_item_at,
            "coderabbit_done_at": self.coderabbit_done_at,
            "coderabbit_state": self.coderabbit_state,
            "coderabbit_reason": self.coderabbit_reason,
            "status": self.status,
            "packet_keys": sorted(list(k) for k in self.packet_keys),
            "sleep_gaps": [list(g) for g in self.sleep_gaps],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RoundState:
        return cls(
            head_sha=d["head_sha"],
            seq=int(d.get("seq", 1)),
            supervised_seconds=float(d.get("supervised_seconds", 0.0)),
            since_push_at_open=float(d.get("since_push_at_open", 0.0)),
            wall_opened_at=d.get("wall_opened_at"),
            seen_keys={tuple(k) for k in d.get("seen_keys", ())},
            last_item_at=d.get("last_item_at"),
            coderabbit_done_at=d.get("coderabbit_done_at"),
            coderabbit_state=d.get("coderabbit_state", CodeRabbitState.PENDING.value),
            coderabbit_reason=d.get("coderabbit_reason", ""),
            status=d.get("status", RoundStatus.WAITING.value),
            packet_keys={tuple(k) for k in d.get("packet_keys", ())},
            sleep_gaps=[tuple(g) for g in d.get("sleep_gaps", ())],
        )

    @property
    def seconds_since_push(self) -> float:
        """For check_appear_timeout and coderabbit_appear_timeout, in supervised time."""
        return self.since_push_at_open + self.supervised_seconds


@dataclass(frozen=True)
class RoundOutcome:
    status: RoundStatus
    waiting_on: tuple[str, ...]
    supervised_seconds: float
    checks: RequiredChecks | None = None
    coderabbit: CodeRabbitStatus | None = None
    packet_keys: frozenset[Key] = frozenset()
    reason: str = ""


class Round:
    def __init__(
        self,
        state: RoundState,
        *,
        clock: Clock,
        round_timeout: float,
        settle: float = SETTLE_SECONDS,
    ):
        self.state = state
        self.clock = clock
        self.round_timeout = round_timeout
        self.settle = settle
        # Process-local baseline (R25): nothing before this object existed is counted.
        self._last_mono = clock.monotonic()
        self._last_wall = clock.wall()
        if state.wall_opened_at is None:
            state.wall_opened_at = self._last_wall

    @classmethod
    def open(cls, head_sha: str, *, clock: Clock, round_timeout: float, seq: int = 1, settle: float = SETTLE_SECONDS) -> Round:
        return cls(RoundState(head_sha=head_sha, seq=seq), clock=clock, round_timeout=round_timeout, settle=settle)

    @property
    def status(self) -> RoundStatus:
        return RoundStatus(self.state.status)

    def tick(self) -> float:
        """Add the monotonic time since the last tick in this process (R25)."""
        mono, wall = self.clock.monotonic(), self.clock.wall()
        delta = max(0.0, mono - self._last_mono)
        gap = (wall - self._last_wall) - delta
        if gap > SLEEP_GAP_SECONDS:
            self.state.sleep_gaps.append((wall, gap))
        self._last_mono, self._last_wall = mono, wall
        self.state.supervised_seconds += delta
        return self.state.supervised_seconds

    def wall_elapsed(self) -> float:
        return self.clock.wall() - (self.state.wall_opened_at or self.clock.wall())

    def observe(
        self,
        *,
        checks: RequiredChecks,
        codex_recorded: bool,
        coderabbit: CodeRabbitStatus,
        pending_keys: Iterable[Key],
    ) -> RoundOutcome:
        """One poll's observations. pending_keys = admitted keys not yet processed."""
        st = self.state
        if self.status is not RoundStatus.WAITING:
            return RoundOutcome(self.status, (), st.supervised_seconds, checks, coderabbit, frozenset(st.packet_keys), "round already ended")
        now = self.tick()
        pending = set(pending_keys)

        if checks.missing:
            st.status = RoundStatus.CHECK_MISSING.value
            names = ", ".join(v.name for v in checks.missing)
            return RoundOutcome(RoundStatus.CHECK_MISSING, (), now, checks, coderabbit, reason=f"required check missing: {names}")

        new = pending - st.seen_keys
        if new:
            st.seen_keys |= new
            st.last_item_at = now
        if coderabbit.done and st.coderabbit_done_at is None:
            st.coderabbit_done_at = now
        st.coderabbit_state = coderabbit.state.value
        st.coderabbit_reason = coderabbit.reason

        waiting = []
        if not checks.concluded:
            waiting.append("required checks: " + ", ".join(v.name for v in checks.pending))
        if not codex_recorded:
            waiting.append("Codex review for the head")
        if not coderabbit.done:
            waiting.append(f"CodeRabbit ({coderabbit.reason})")
        else:
            anchor = max(st.coderabbit_done_at or 0.0, st.last_item_at or 0.0)
            if now - anchor < self.settle:
                waiting.append(f"settle ({self.settle - (now - anchor):.0f} s left)")

        if not waiting:
            st.status = RoundStatus.COMPLETE.value
            st.packet_keys = set(pending)
            return RoundOutcome(RoundStatus.COMPLETE, (), now, checks, coderabbit, frozenset(pending))
        if now >= self.round_timeout:
            st.status = RoundStatus.TIMED_OUT.value
            return RoundOutcome(
                RoundStatus.TIMED_OUT, tuple(waiting), now, checks, coderabbit, reason=f"not complete after {int(self.round_timeout)} supervised seconds"
            )
        return RoundOutcome(RoundStatus.WAITING, tuple(waiting), now, checks, coderabbit)

    def late_keys(self, admitted_keys: Iterable[Key], processed_keys: Iterable[Key]) -> frozenset[Key]:
        """Items admitted after this round completed that its packet did not hold."""
        if self.status is not RoundStatus.COMPLETE:
            return frozenset()
        return frozenset(set(admitted_keys) - set(processed_keys) - self.state.packet_keys)

    def open_late_round(self, late: Iterable[Key]) -> Round:
        """Round seq+1 on the same head, opened at its first late item (R8)."""
        late = set(late)
        if not late:
            raise ValueError("a seq+1 round opens only on late items")
        st = RoundState(
            head_sha=self.state.head_sha,
            seq=self.state.seq + 1,
            # This round stopped ticking when it completed; count the time since, in this process.
            since_push_at_open=self.state.seconds_since_push + max(0.0, self.clock.monotonic() - self._last_mono),
            seen_keys=late,
            last_item_at=0.0,
        )
        return Round(st, clock=self.clock, round_timeout=self.round_timeout, settle=self.settle)


# --- polling glue -------------------------------------------------------------------


@dataclass(frozen=True)
class PRSnapshot:
    head_sha: str
    pull: Mapping[str, Any]
    statuses: list[Mapping[str, Any]]
    check_runs: list[Mapping[str, Any]]
    reviews: list[Mapping[str, Any]]
    review_comments: list[Mapping[str, Any]]
    issue_comments: list[Mapping[str, Any]]
    review_threads: list[Mapping[str, Any]]

    @property
    def remote_head(self) -> str:
        return str(((self.pull.get("head") or {}).get("sha")) or "")


def fetch_snapshot(gh: GitHubReader, repo: str, number: int, head_sha: str) -> PRSnapshot:
    """The 7 reads per poll from Experiment 2 section 6. Any GitHubError propagates."""
    return PRSnapshot(
        head_sha=head_sha,
        pull=gh.pull(repo, number),
        statuses=gh.commit_statuses(repo, head_sha),
        check_runs=gh.check_runs(repo, head_sha),
        reviews=gh.reviews(repo, number),
        review_comments=gh.review_comments(repo, number),
        issue_comments=gh.issue_comments(repo, number),
        review_threads=gh.review_threads(repo, number),
    )


@dataclass(frozen=True)
class Observation:
    checks: RequiredChecks
    coderabbit: CodeRabbitStatus
    admission: Admission
    pending_keys: frozenset[Key]


def evaluate_snapshot(
    snap: PRSnapshot,
    *,
    repo: RepoConfig,
    operator_login: str,
    processed_keys: Iterable[Key],
    seconds_since_push: float,
) -> Observation:
    checks = evaluate_required_checks(
        repo.required_checks,
        repo.skippable_checks,
        snap.check_runs,
        statuses=snap.statuses,
        seconds_since_push=seconds_since_push,
        appear_timeout=repo.check_appear_timeout,
    )
    cr = coderabbit_status(snap.statuses, seconds_since_push=seconds_since_push, appear_timeout=repo.coderabbit_appear_timeout)
    adm = admit(
        head_sha=snap.head_sha,
        operator_login=operator_login,
        reviews=snap.reviews,
        review_comments=snap.review_comments,
        issue_comments=snap.issue_comments,
        review_threads=snap.review_threads,
    )
    return Observation(checks, cr, adm, frozenset(adm.keys - set(processed_keys)))


def poll_round(
    rnd: Round,
    gh: GitHubReader,
    *,
    repo: RepoConfig,
    pr_number: int,
    operator_login: str,
    processed_keys: Iterable[Key],
    codex_recorded: bool,
) -> tuple[RoundOutcome, Observation]:
    """One AI-free poll. GitHubError and IncompleteData propagate: a failed poll
    never counts as an empty or completed review; supervised time still runs."""
    rnd.tick()
    snap = fetch_snapshot(gh, repo.github, pr_number, rnd.state.head_sha)
    obs = evaluate_snapshot(
        snap,
        repo=repo,
        operator_login=operator_login,
        processed_keys=processed_keys,
        seconds_since_push=rnd.state.seconds_since_push,
    )
    out = rnd.observe(checks=obs.checks, codex_recorded=codex_recorded, coderabbit=obs.coderabbit, pending_keys=obs.pending_keys)
    return out, obs
