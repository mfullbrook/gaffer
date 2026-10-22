"""R8 rerun semantics: --more-cycles, --adopt-head, --continue, --fresh, and
reruns of BLOCKED, READY and CLOSED jobs."""

from __future__ import annotations

from pathlib import Path

import pytest

from gaffer import runner as rn

from runner_fakes import BAD_SOURCE, GOOD_SOURCE, World, WriterCall, address_fix, commit, implement_bad
from workspace_helpers import run_git


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# --- --more-cycles -----------------------------------------------------------------


def _still_bad(world: World):
    def act(call: WriterCall):
        sha = commit(call.clone, {"src/app/__init__.py": BAD_SOURCE + f"# {len(world.children.calls)}\n"})
        return {"status": "done", "summary": "tried", "question": None,
                "dispositions": [{"item_id": i, "disposition": "fixed", "commit_sha": sha, "reason": "x"} for i in call.item_ids]}

    return act


def test_more_cycles_grants_n_recorded_cycles_after_verify_failed(world):
    world.children.default_implement = implement_bad
    world.children.default_address = _still_bad(world)
    assert world.run()[0] == rn.EXIT_BLOCKED
    assert world.job().blocked_reason == "verify_failed" and world.job().cycles_used == 3

    # Without the flag the cycle count persists: still blocked, no extra resume.
    calls = len(world.children.calls)
    assert world.run()[0] == rn.EXIT_BLOCKED
    assert len(world.children.calls) == calls and world.job().cycles_used == 3

    world.children.default_address = address_fix
    code, out = world.run(more_cycles=1)
    assert code == rn.EXIT_READY, out
    job = world.job()
    assert (job.cycles_used, job.cycle_limit) == (4, 4)
    with world.store() as s:
        assert [n for n, _, _ in s.cycle_grants(job.id)] == [1]
    assert "granted 1 more repair cycle" in out
    assert "Repair cycles: 4 of 4 (granted with --more-cycles: 1)" in world.report()


def test_more_cycles_after_repair_limit_resumes_the_pending_items(world):
    seen: set = set()

    def hook(gh, n):
        head = gh.remote_sha("gaffer/ENG-1")
        if head not in seen and len(seen) < 4:
            seen.add(head)
            gh.add_inline(f"nit {len(seen)}")

    world.gh.on_poll = hook
    assert world.run()[0] == rn.EXIT_BLOCKED and world.job().blocked_reason == "repair_limit"
    code, out = world.run(more_cycles=1)
    assert code == rn.EXIT_READY, out
    assert "nit 4" in [c for c in world.children.calls if c.kind == "resume"][-1].prompt


# --- --adopt-head ------------------------------------------------------------------


def _mark_pushes_mid_round(world: World) -> dict:
    state: dict = {}

    def hook(gh, n):
        if "sha" not in state:
            state["sha"] = world.mark_pushes({"README.md": "mark's tweak\n"})

    world.gh.on_poll = hook
    return state


def test_adopt_head_accepts_marks_commit_and_recomputes_the_base(world):
    state = _mark_pushes_mid_round(world)
    assert world.run()[0] == rn.EXIT_BLOCKED and world.job().blocked_reason == "foreign_head"
    # A plain rerun re-validates and blocks again.
    code, out = world.run()
    assert code == rn.EXIT_BLOCKED and "--adopt-head" in out
    code, out = world.run(adopt_head=True)
    assert code == rn.EXIT_READY, out
    job = world.job()
    assert job.head_sha == state["sha"] == world.remote_head()
    assert job.base_sha == run_git(world.remote, "merge-base", "main", state["sha"])
    assert world.events("adopt_head")
    assert f"--adopt-head: accepted `{state['sha']}`" in world.report()
    assert len(world.gh.pulls) == 1


def test_adopt_head_after_base_conflict(world):
    def hook(gh, n):
        if "merged" not in flags:
            gh.pr()["mergeable"] = False

    flags: dict = {}
    world.gh.on_poll = hook
    assert world.run()[0] == rn.EXIT_BLOCKED and world.job().blocked_reason == "base_conflict"
    world.mark_pushes({"README.md": "resolved\n"}, message="merge main")
    flags["merged"] = True
    world.gh.pr()["mergeable"] = True
    assert world.run(adopt_head=True)[0] == rn.EXIT_READY


def test_adopt_head_refuses_when_nothing_to_adopt(world):
    assert world.run()[0] == rn.EXIT_READY
    code, out = world.run(adopt_head=True)
    assert code == rn.EXIT_REFUSED and "foreign_head or base_conflict" in out


def test_adopt_head_on_state_lost_is_refused_until_reconstruction_exists(world):
    run_git(world.remote, "branch", "gaffer/ENG-1", "main")
    assert world.run()[0] == rn.EXIT_BLOCKED
    code, out = world.run(adopt_head=True)
    assert code == rn.EXIT_REFUSED and "isn't built yet" in out


# --- READY and --continue ------------------------------------------------------------


def test_continue_processes_feedback_after_ready_as_a_new_round(world):
    assert world.run()[0] == rn.EXIT_READY
    head = world.job().head_sha
    world.gh.add_issue_comment(world.gh.pr()["number"], "Mark: one more thing, please log the result.")
    # A plain READY rerun ignores it and doesn't touch GitHub.
    requests = list(world.gh.requests)
    assert world.run()[0] == rn.EXIT_READY and world.gh.requests == requests
    code, out = world.run(cont=True)
    assert code == rn.EXIT_READY, out
    resume = [c for c in world.children.calls if c.kind == "resume"]
    assert len(resume) == 1 and "please log the result" in resume[0].prompt
    job = world.job()
    assert job.head_sha != head and job.cycles_used == 1
    with world.store() as s:
        rounds = s.rounds(job.id)
    assert [(r.head_sha == head, r.seq) for r in rounds] == [(True, 1), (True, 2), (False, 1)]


@pytest.mark.parametrize("opts", [{"cont": True}, {"fresh": True, "cont": True}])
def test_continue_needs_a_ready_job(world, opts):
    code, out = world.run(**opts)
    assert code == rn.EXIT_REFUSED


# --- CLOSED and --fresh --------------------------------------------------------------


def _close_mid_round(world: World):
    def hook(gh, n):
        if gh.pr()["number"] == 100:
            gh.pr()["state"] = "closed"

    world.gh.on_poll = hook


def test_closed_rerun_refuses_and_fresh_starts_generation_2(world):
    _close_mid_round(world)
    assert world.run()[0] == rn.EXIT_CLOSED
    code, out = world.run()
    assert code == rn.EXIT_CLOSED and "--fresh" in out and "gaffer/ENG-1-2" in out
    world.gh.on_poll = None
    code, out = world.run(fresh=True)
    assert code == rn.EXIT_READY, out
    job = world.job()
    assert (job.generation, job.branch, rn.job_marker(job)) == (2, "gaffer/ENG-1-2", "ENG-1.2")
    pr = world.gh.pr()
    assert pr["number"] == 101 and pr["head"]["ref"] == "gaffer/ENG-1-2"
    assert "gaffer-job: ENG-1.2" in pr["body"].splitlines()
    markers = [l for c in world.gh.comments.get(101, []) for l in c["body"].splitlines() if l.startswith("gaffer-round: ")]
    assert markers == [f"gaffer-round: ENG-1.2/{job.head_sha}/1"]
    with world.store() as s:
        old = [j for j in s.jobs_for_issue("ENG-1") if j.generation == 1][0]
    assert old.archived_at is not None and old.state == "CLOSED"
    assert Path(job.clone_path).parent.name == "ENG-1.2"


@pytest.mark.parametrize("prepare", ["ready", "blocked"])
def test_fresh_refuses_unless_closed(world, prepare):
    if prepare == "blocked":
        world.linear.issues["ENG-1"] = rn.Issue("ENG-1", "x", "no criteria")
    world.run()
    code, out = world.run(fresh=True)
    assert code == rn.EXIT_REFUSED and "only a CLOSED job" in out


# --- BLOCKED reruns re-validate from the blocked step ----------------------------------


def test_blocked_rerun_revalidates_from_intake_after_the_issue_is_fixed(world):
    world.linear.issues["ENG-1"] = rn.Issue("ENG-1", "Vague", "Make it better.")
    assert world.run()[0] == rn.EXIT_BLOCKED
    world.linear.issues["ENG-1"] = rn.Issue("ENG-1", "Add feature", "## Acceptance criteria\n- feature() returns 42\n")
    assert world.run()[0] == rn.EXIT_READY
    assert world.linear.calls == ["ENG-1", "ENG-1"]


def test_review_timeout_rerun_opens_a_new_round_on_the_same_head(world):
    world.gh.check_runs_for = lambda sha: [{"id": 1, "name": "test", "status": "queued", "conclusion": None}]
    assert world.run()[0] == rn.EXIT_BLOCKED and world.job().blocked_reason == "review_timeout"
    world.gh.check_runs_for = world.gh.default_check_runs
    assert world.run()[0] == rn.EXIT_READY
    with world.store() as s:
        rounds = s.rounds(world.job().id)
    assert [(r.seq, r.outcome) for r in rounds] == [(1, "timed_out"), (2, "complete")]


def test_writer_blocked_rerun_resumes_the_same_session(world):
    world.children.writer_actions = [lambda c: {"status": "blocked", "summary": "?", "question": "Which unit?"}]
    assert world.run()[0] == rn.EXIT_BLOCKED
    assert world.run()[0] == rn.EXIT_READY
    calls = world.children.calls
    assert [c.kind for c in calls] == ["launch", "resume"] and calls[0].session_id == calls[1].session_id
