"""R34: a child that fails with HTTP 429/529 is retried after growing waits (30 s, 2 min, 5 min),
then treated as a usage limit. Runner level, with fakes: the Codex tester and reviewer, the Claude
writer (launch and packet resume), and how the backoff sits beside each step's own retries, the
nudge, the fresh-writer fallback and the repair-cycle budget."""

from __future__ import annotations

from pathlib import Path

import pytest

from gaffer import children as ch
from gaffer import runner as rn

from runner_fakes import (
    THROTTLED,
    TESTER_OUTPUT,
    World,
    address_fix,
    implement_bad,
    implement_good,
    writer_throttled,
)

WAITS = [30.0, 120.0, 300.0]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def backoffs(world: World) -> list[tuple[str, str, float]]:
    return [(e.detail["provider"], e.detail["role"], e.detail["wait"]) for e in world.events("throttled")]


def outcomes(world: World, role: str) -> list[str | None]:
    with world.store() as s:
        return [a.outcome for a in s.attempts(world.job().id) if a.role == role]


def test_default_waits_are_about_30s_2min_5min():
    assert ch.THROTTLE_BACKOFF == (30.0, 120.0, 300.0)
    assert rn.Deps.__dataclass_fields__["backoff"].default == ch.THROTTLE_BACKOFF


# --- Codex tester ---------------------------------------------------------------------------


def test_tester_429_once_waits_30s_and_is_not_the_testers_retry(world):
    world.children.tester_outputs = [THROTTLED]
    code, out = world.run()
    assert code == rn.EXIT_READY, out
    assert backoffs(world) == [("codex", "tester", 30.0)]
    assert world.sleeps[0] == 30.0
    assert world.children.tester_calls == 2
    # The same prompt both times: a 429 isn't fed back as the tester's retry error.
    assert world.children.tester_prompts[0] == world.children.tester_prompts[1]
    assert outcomes(world, "tester") == ["throttled", "ok"]


def test_tester_still_429_after_every_wait_is_a_usage_limit(world):
    world.children.tester_outputs = [THROTTLED] * 4
    code, out = world.run()
    job = world.job()
    assert (code, job.blocked_reason, job.blocked_step) == (rn.EXIT_BLOCKED, "usage_limit", "TESTER"), out
    assert backoffs(world) == [("codex", "tester", w) for w in WAITS]
    assert world.sleeps[:3] == WAITS
    assert world.children.tester_calls == 4
    assert "codex usage limit: the tester was still throttled (HTTP 429/529) after 3 backoff retries" in job.blocked_message
    assert outcomes(world, "tester") == ["throttled"] * 4


def test_tester_backoff_then_its_own_retry_both_still_available(world):
    # 429 -> wait -> an ordinary error (the tester's one retry) -> 429 -> wait (budget is per run) -> ok
    world.children.tester_outputs = [THROTTLED, "error", THROTTLED, TESTER_OUTPUT]
    code, out = world.run()
    assert code == rn.EXIT_READY, out
    assert backoffs(world) == [("codex", "tester", 30.0), ("codex", "tester", 30.0)]
    assert "tester exited 1 without a result" in world.children.tester_prompts[2]  # the real retry carries its error


def test_two_ordinary_tester_errors_still_block_after_a_backoff(world):
    world.children.tester_outputs = [THROTTLED, "error", "error"]
    code, _ = world.run()
    assert world.job().blocked_reason == "tester_failed"


def test_a_real_codex_limit_is_not_backed_off(world):
    world.children.tester_outputs = [ch.LimitEvent("codex", "rate limit reached (primary)", 1_760_100_000)]
    world.run()
    assert world.job().blocked_reason == "usage_limit" and backoffs(world) == [] and world.children.tester_calls == 1


def test_no_waits_means_a_429_is_a_limit_at_once(world):
    world.children.tester_outputs = [THROTTLED]
    store = world.store()
    try:
        import io

        deps = world.deps(store, io.StringIO())
        deps.backoff = ()
        code = rn.Runner(deps).run("ENG-1", rn.Options())
    finally:
        store.close()
    assert code == rn.EXIT_BLOCKED and world.job().blocked_reason == "usage_limit"
    assert world.children.tester_calls == 1 and backoffs(world) == []


# --- Codex reviewer -------------------------------------------------------------------------


def test_reviewer_429_twice_then_ok_is_one_review_attempt(world):
    world.children.reviews = [THROTTLED, THROTTLED]
    code, out = world.run()
    assert code == rn.EXIT_READY, out
    assert backoffs(world) == [("codex", "reviewer", 30.0), ("codex", "reviewer", 120.0)]
    assert world.children.reviewer_calls == 3
    assert outcomes(world, "reviewer") == ["throttled", "throttled", "ok"]


def test_reviewer_still_429_is_a_usage_limit_not_reviewer_failed(world):
    world.children.reviews = [THROTTLED] * 4
    code, _ = world.run()
    job = world.job()
    assert (job.blocked_reason, job.blocked_step) == ("usage_limit", "PUBLISH")
    assert world.children.reviewer_calls == 4 and backoffs(world) == [("codex", "reviewer", w) for w in WAITS]


def test_reviewer_keeps_its_two_real_attempts_beside_the_backoff(world):
    # A 429 retried after a wait, then two ordinary failures: still reviewer_failed, after 3 calls.
    world.children.reviews = [THROTTLED, "error", "error"]
    world.run()
    assert world.job().blocked_reason == "reviewer_failed"
    assert world.children.reviewer_calls == 3


def test_reviewer_error_then_429_until_the_limit(world):
    world.children.reviews = ["error", *[THROTTLED] * 4]
    world.run()
    assert world.job().blocked_reason == "usage_limit" and world.children.reviewer_calls == 5


# --- Claude writer --------------------------------------------------------------------------


def test_writer_529_resumes_the_same_session_with_continue(world):
    world.children.writer_actions = [writer_throttled, implement_good]
    code, out = world.run()
    assert code == rn.EXIT_READY, out
    calls = world.children.calls
    assert [c.kind for c in calls] == ["launch", "resume"]
    assert calls[1].session_id == calls[0].session_id and calls[1].prompt == ch.CONTINUE_PROMPT
    assert backoffs(world) == [("claude", "writer", 30.0)]
    assert world.events("fresh_writer") == [] and world.events("writer_nudge") == []
    assert world.job().cycles_used == 0
    assert outcomes(world, "writer") == ["throttled", "ok"]


def test_writer_still_529_is_a_usage_limit_and_a_rerun_continues(world):
    world.children.writer_actions = [writer_throttled] * 4
    code, _ = world.run()
    job = world.job()
    assert (code, job.blocked_reason, job.blocked_step) == (rn.EXIT_BLOCKED, "usage_limit", "WRITER")
    assert "claude usage limit: the writer was still throttled" in job.blocked_message
    assert backoffs(world) == [("claude", "writer", w) for w in WAITS]
    assert len({c.session_id for c in world.children.calls}) == 1
    # The existing usage-limit path: a rerun resumes the dead writer with "continue" (recovery table).
    world.children.writer_actions = [implement_good]
    code, out = world.run()
    assert code == rn.EXIT_READY, out
    assert world.children.calls[-1].kind == "resume" and world.children.calls[-1].prompt == ch.CONTINUE_PROMPT


def test_packet_resume_529_is_retried_with_the_same_packet_and_no_extra_cycle(world):
    def throttled_address(call):
        return writer_throttled(call)

    world.children.writer_actions = [implement_bad, throttled_address, address_fix]
    code, out = world.run()
    assert code == rn.EXIT_READY, out
    calls = world.children.calls
    assert [c.kind for c in calls] == ["launch", "resume", "resume"]
    assert calls[2].prompt == calls[1].prompt and calls[2].item_ids == calls[1].item_ids
    assert world.job().cycles_used == 1  # the one local repair (R24), not two
    assert backoffs(world) == [("claude", "writer", 30.0)]
