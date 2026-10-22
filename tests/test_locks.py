"""R9 locks and liveness, R31 crashed-active-job refusal, and the other exit-4
refusals of `gaffer run`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from gaffer import children as ch
from gaffer import runner as rn

from runner_fakes import GOOD_SOURCE, Crash, World, WriterCall, commit

HOLD = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, f"{os.getpid()} {sys.argv[2]}\\n".encode())
print("held", flush=True)
time.sleep(120)
"""


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def holder():
    procs = []

    def hold(path: Path, key: str) -> subprocess.Popen:
        path.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen([sys.executable, "-c", HOLD, str(path), key], stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "held"
        procs.append(p)
        return p

    yield hold
    for p in procs:
        p.kill()
        p.wait()


def test_second_invocation_for_the_same_job_exits_4_naming_the_holder(world, holder):
    p = holder(rn.job_lock_path(world.home, "ENG-1"), "ENG-1")
    code, out = world.run()
    assert code == rn.EXIT_REFUSED
    assert f"pid {p.pid}" in out and "already driving this job" in out
    assert world.linear.calls == [] and world.children.tester_calls == 0


def test_run_lock_held_by_another_job_exits_4_naming_pid_and_job(world, holder):
    p = holder(rn.run_lock_path(world.home), "ENG-2")
    code, out = world.run()
    assert code == rn.EXIT_REFUSED
    assert f"pid {p.pid} for ENG-2" in out and "one job at a time" in out


def test_locks_are_released_after_a_run_and_after_a_crash(world):
    world.children.tester_outputs = [Crash]
    assert world.run()[0] == "crash"
    assert world.run()[0] == rn.EXIT_READY
    for path in (rn.job_lock_path(world.home, "ENG-1"), rn.run_lock_path(world.home)):
        with path.open("r+") as f:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free again


def test_lock_file_names_the_holder_while_held(world):
    seen = {}

    def act(call: WriterCall):
        seen["job"] = rn.job_lock_path(world.home, "ENG-1").read_text()
        seen["run"] = rn.run_lock_path(world.home).read_text()
        commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE})
        return {"status": "done", "summary": "x", "question": None}

    world.children.writer_actions = [act]
    world.run()
    import os

    assert seen == {"job": f"{os.getpid()} ENG-1\n", "run": f"{os.getpid()} ENG-1\n"}


def test_crashed_active_job_refuses_other_issues_naming_the_job_and_the_fix(world):
    def crash(call: WriterCall):
        raise Crash()

    world.children.writer_actions = [crash]
    assert world.run()[0] == "crash"
    code, out = world.run("ENG-2")
    assert code == rn.EXIT_REFUSED
    assert "job ENG-1 is still active (state WRITER)" in out
    assert "`gaffer run ENG-1`" in out and "`gaffer stop ENG-1`" in out
    assert world.job("ENG-2") is None
    # Finishing ENG-1 frees the slot (never auto-parked, R31).
    assert world.run()[0] == rn.EXIT_READY
    assert world.run("ENG-2")[0] == rn.EXIT_READY


def test_live_writer_child_refuses_and_no_second_writer_starts(world):
    def crash(call: WriterCall):
        raise Crash()

    world.children.writer_actions = [crash]
    world.run()
    sleeper = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    try:
        with world.store() as s:
            job = s.job_for_issue("ENG-1")
            for lease in s.live_leases(job.id):
                s.release_lease(lease.id, "test")
            s.acquire_lease(job.id, role="writer", pid=sleeper.pid, proc_start=ch.process_start_time(sleeper.pid),
                            pgid=sleeper.pid, session_id=job.writer_session_id)
        calls = len(world.children.calls)
        code, out = world.run()
        assert code == rn.EXIT_REFUSED
        assert f"writer pid {sleeper.pid}" in out and "won't start a second one" in out
        assert len(world.children.calls) == calls
    finally:
        sleeper.kill()
        sleeper.wait()
    # Once it's gone, the rerun resumes the same session.
    assert world.run()[0] == rn.EXIT_READY
    assert world.children.calls[-1].kind == "resume"


def test_dead_writer_leader_with_a_live_group_is_sigtermed_on_rerun(world):
    def crash(call: WriterCall):
        raise Crash()

    world.children.writer_actions = [crash]
    world.run()
    with world.store() as s:
        lease = s.live_leases(s.job_for_issue("ENG-1").id, role="writer")[0]
    world.children.alive_groups.add(lease.pgid)
    assert world.run()[0] == rn.EXIT_READY
    assert lease.pgid in world.children.terminated
    assert world.events("writer_group_sigterm")


@pytest.mark.parametrize("key", ["eng-1", "ENG-1\n", "ENG-", "ENG-1; rm -rf /", "1ENG-2"])
def test_bad_issue_keys_are_refused(world, key):
    code, out = world.run(key)
    assert code == rn.EXIT_REFUSED and "not a Linear issue key" in out
    assert not rn.run_lock_path(world.home).exists()


def test_unmapped_team_is_refused(world):
    code, out = world.run("OPS-3")
    assert code == rn.EXIT_REFUSED and "[repos.OPS]" in out


def test_preflight_failure_is_refused_and_creates_no_job(world):
    world.children.preflight_error = ["claude auth status: authMethod is 'console', need 'claude.ai' (R2)."]
    code, out = world.run()
    assert code == rn.EXIT_REFUSED and "authMethod" in out
    assert world.job() is None


def test_cli_parser_refusals_exit_4(capsys):
    for argv in (["run"], ["run", "ENG-1", "--more-cycles", "x"], ["nope"]):
        with pytest.raises(SystemExit) as e:
            rn.build_parser().parse_args(argv)
        assert e.value.code == rn.EXIT_REFUSED
    assert rn.main(["run", "eng-1"]) == rn.EXIT_REFUSED
    assert rn.main(["run", "ENG-1", "--more-cycles", "-2"]) == rn.EXIT_REFUSED
