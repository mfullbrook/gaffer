"""T9 runner: the job state machine end to end with fakes (children, GitHub,
Linear, clock) around disposable git repos. Covers the job flow, every BLOCKED
reason the runner can reach, the local repair loop (R24), rounds and packets
(D2, R9, R27, R31), the report, the read-only Linear client (D1) and every row
of the recovery table (SC4)."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from gaffer import children as ch
from gaffer import config as gconfig
from gaffer import runner as rn
from gaffer import workspace as ws_mod
from gaffer.config import Secret

from runner_fakes import (
    BAD_SOURCE,
    FEATURE_TEST,
    GOOD_SOURCE,
    INIT,
    TESTER_OUTPUT,
    Crash,
    World,
    WriterCall,
    address_decline,
    address_fix,
    commit,
    implement_bad,
    implement_good,
    writer_run,
)
from workspace_helpers import make_config, run_git


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def branch_log(world: World, branch: str = "gaffer/ENG-1") -> list[str]:
    return run_git(world.remote, "log", "--format=%s", branch).splitlines()


# --- the happy path ---------------------------------------------------------------


def test_happy_path_is_ready_with_one_pr_one_writer_and_one_round_comment(world):
    code, out = world.run()
    assert code == rn.EXIT_READY, out
    job = world.job()
    assert job.state == "READY"
    assert len(world.gh.pulls) == 1
    pr = world.gh.pr()
    assert pr["draft"] is False and pr["title"] == "ENG-1: Add feature"
    assert "gaffer-job: ENG-1" in pr["body"].splitlines()
    assert [c.kind for c in world.children.calls] == ["launch"]
    assert world.remote_head() == job.head_sha
    # The tester commit is the branch's first commit (design TESTER).
    assert branch_log(world)[-2] == "ENG-1: tester tests"
    comments = world.gh.round_comments()
    assert len(comments) == 1
    assert f"gaffer-round: ENG-1/{job.head_sha}/1" in comments[0]["body"].splitlines()
    assert world.linear.calls == ["ENG-1"]
    report = world.report()
    assert "READY" in report and "CodeRabbit on the head: verified" in report
    assert "| C1: feature() returns 42 | `tests/test_feature.py::tests.test_feature::test_feature` | acceptance | passed |" in report
    assert "Synced skills the writer saw (R28): address-pr-feedback, code-loop" in report
    assert "`.git/config`: never changed" in report
    assert "paid evaluations: not covered" in report


def test_round_waits_for_the_settle_and_polls_with_jitter(world):
    world.run()
    assert world.sleeps, "the round should wait at least one poll for the 60 s settle"
    assert all(60 <= s <= 70 for s in world.sleeps)


def test_ready_rerun_reprints_the_report_without_touching_github_or_linear(world):
    assert world.run()[0] == 0
    before = (list(world.gh.requests), list(world.linear.calls), len(world.children.calls), world.children.tester_calls)
    code, out = world.run()
    assert code == rn.EXIT_READY
    assert "# gaffer report: ENG-1 — READY" in out
    assert (world.gh.requests, world.linear.calls, len(world.children.calls), world.children.tester_calls) == before


def test_no_writer_is_alive_during_round_and_a_lingering_group_is_sigtermed(world):
    def lingering(call: WriterCall):
        commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE})
        return writer_run(call.session_id, ch.ChildLease("writer", 1, 1, "x", "", call.session_id, 0.0),
                          {"status": "done", "summary": "ok", "question": None}, group_terminated=True)

    world.children.writer_actions = [lingering]
    assert world.run()[0] == 0
    assert world.events("writer_group_sigterm")
    assert "outlived the 2 min grace and was SIGTERMed" in world.report()


# --- BLOCKED reasons ----------------------------------------------------------------


def _no_ac(w: World):
    w.linear.issues["ENG-1"] = rn.Issue("ENG-1", "Vague", "Make it better somehow.")


def _tester_errors(w: World):
    w.children.tester_outputs = ["error", "error"]


def _tester_bad_path(w: World):
    bad = json.loads(json.dumps(TESTER_OUTPUT))
    bad["files"][0]["path"] = "src/evil.py"
    w.children.tester_outputs = [bad, bad]


def _tester_wrong_red(w: World):
    bad = json.loads(json.dumps(TESTER_OUTPUT))
    bad["expected_missing"] = ["app.something_else"]
    w.children.tester_outputs = [bad, bad]


def _already_passing(w: World):
    w.mark_pushes({"src/app/__init__.py": GOOD_SOURCE}, branch="main", message="feature already there")


def _untestable(w: World):
    out = json.loads(json.dumps(TESTER_OUTPUT))
    out["untestable"] = [{"criterion_id": "C1", "reason": "needs a browser"}]
    out["tests"] = []
    w.children.tester_outputs = [out]


def _writer_no_result(w: World):
    w.children.writer_actions = [lambda c: writer_run(c.session_id, ch.ChildLease("writer", 1, 1, "x", "", None, 0.0),
                                                      None, error="writer exited 1 without a result event")]


def _writer_blocked(w: World):
    w.children.writer_actions = [lambda c: {"status": "blocked", "summary": "stuck", "question": "Should feature() be async?"}]


def _writer_dirty(w: World):
    def dirty(call: WriterCall):
        (call.clone / "src/app/__init__.py").write_text(GOOD_SOURCE)
        return {"status": "done", "summary": "done (forgot to commit)", "question": None}

    w.children.writer_actions = [dirty, dirty]


def _writer_no_change(w: World):
    w.children.writer_actions = [lambda c: {"status": "done", "summary": "nothing to do", "question": None}]


def _forbidden(w: World):
    def act(call: WriterCall):
        commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE, ".claude/settings.json": "{}"})
        return {"status": "done", "summary": "x", "question": None}

    w.children.writer_actions = [act]


def _unexplained_harness(w: World):
    def act(call: WriterCall):
        commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE, "pyproject.toml": "[project]\nname = 'app2'\n"})
        return {"status": "done", "summary": "x", "question": None}

    w.children.writer_actions = [act]


def _branch_moved(w: World):
    def act(call: WriterCall):
        commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE})
        run_git(call.clone, "checkout", "-q", "--detach")
        return {"status": "done", "summary": "x", "question": None}

    w.children.writer_actions = [act]


def _verify_never_passes(w: World):
    def still_bad(call: WriterCall):
        sha = commit(call.clone, {"src/app/__init__.py": BAD_SOURCE + f"# try {len(w.children.calls)}\n"})
        return {"status": "done", "summary": "tried", "question": None,
                "dispositions": [{"item_id": i, "disposition": "fixed", "commit_sha": sha, "reason": "x"} for i in call.item_ids]}

    w.children.default_implement = implement_bad
    w.children.default_address = still_bad


def _usage_limit(w: World):
    w.children.tester_outputs = [ch.LimitEvent("codex", "rate limit reached (primary)", 1_760_100_000)]


def _reviewer_fails(w: World):
    w.children.reviews = ["error", "error"]


def _never_concludes(w: World):
    w.gh.check_runs_for = lambda sha: [{"id": 77, "name": "test", "status": "in_progress", "conclusion": None,
                                        "app": {"slug": "github-actions"}, "output": {}}]


def _check_missing(w: World):
    w.gh.check_runs_for = lambda sha: []


def _secret(w: World):
    def act(call: WriterCall):
        commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE + "TOKEN = 'FAKESECRET_ABCDEFGHIJKL'\n"})
        return {"status": "done", "summary": "x", "question": None}

    w.children.writer_actions = [act]


def _plugin_twice(w: World):
    base = {**INIT}
    w.children.init_failures = [{**base, "plugins": [{"name": "a", "source": "plugin-a@synced"}]},
                                {**base, "plugins": [{"name": "b", "source": "plugin-b@synced"}]}]


def _unlisted_mcp(w: World):
    w.children.init_failures = [{**INIT, "mcp_servers": [{"name": "linear"}]}]


def _foreign_head(w: World):
    def hook(gh, n):
        if n == 1:
            w.mark_pushes({"README.md": "mark was here\n"})

    w.gh.on_poll = hook


def _base_conflict(w: World):
    def hook(gh, n):
        gh.pr()["mergeable"] = False

    w.gh.on_poll = hook


def _not_ready(w: World):
    w.gh.check_runs_for = lambda sha: [{"id": 88, "name": "test", "status": "completed", "conclusion": "failure",
                                        "app": {"slug": "github-actions"}, "html_url": "https://ci/88",
                                        "output": {"title": "tests failed", "summary": "1 failed", "annotations_count": 0}}]
    w.gh.logs[88] = b"\x1b[31mFAILED\x1b[0m tests/test_flaky.py::test_x\n"
    w.children.default_address = address_decline


def _repair_limit(w: World):
    def hook(gh, n):
        head = gh.remote_sha("gaffer/ENG-1")
        if head not in seen:
            seen.add(head)
            gh.add_inline(f"Please handle the None case ({len(seen)}).")

    seen: set = set()
    w.gh.on_poll = hook


def _foreign_pr(w: World):
    w.gh.pulls[7] = {"number": 7, "html_url": "https://github.com/x/pull/7", "state": "open", "merged": False,
                     "draft": False, "body": "someone else's PR", "title": "x", "head": {"ref": "gaffer/ENG-1"},
                     "base": {"ref": "main"}, "mergeable": True}


BLOCKED_CASES = [
    ("acceptance_criteria_missing", _no_ac, "INTAKE"),
    ("tester_failed", _tester_errors, "TESTER"),
    ("tester_invalid", _tester_bad_path, "TESTER"),
    ("tester_invalid", _tester_wrong_red, "RED_FIRST"),
    ("already_passing", _already_passing, "RED_FIRST"),
    ("no_testable_criteria", _untestable, "TESTER"),
    ("writer_no_result", _writer_no_result, "WRITER"),
    ("writer_blocked", _writer_blocked, "WRITER"),
    ("writer_dirty", _writer_dirty, "WRITER"),
    ("writer_no_change", _writer_no_change, "WRITER"),
    ("forbidden_path", _forbidden, "WRITER"),
    ("tester_change_unexplained", _unexplained_harness, "WRITER"),
    (ws_mod.WRITER_BRANCH_MOVED, _branch_moved, "WRITER"),
    ("verify_failed", _verify_never_passes, "VERIFY"),
    ("usage_limit", _usage_limit, "TESTER"),
    ("reviewer_failed", _reviewer_fails, "PUBLISH"),
    ("review_timeout", _never_concludes, "ROUND"),
    ("required_check_missing", _check_missing, "ROUND"),
    ("secret_detected", _secret, "PUBLISH"),
    ("isolation_failed", _plugin_twice, "WRITER"),
    ("isolation_failed", _unlisted_mcp, "WRITER"),
    ("foreign_head", _foreign_head, "ROUND"),
    ("base_conflict", _base_conflict, "ROUND"),
    ("not_ready", _not_ready, "ROUND"),
    ("repair_limit", _repair_limit, "PACKET"),
    (ws_mod.FOREIGN_PR, _foreign_pr, "PUBLISH"),
]


@pytest.mark.parametrize("reason, setup, step", BLOCKED_CASES, ids=[f"{c[0]}-{c[1].__name__}" for c in BLOCKED_CASES])
def test_blocked_reasons(world, reason, setup, step):
    setup(world)
    code, out = world.run()
    job = world.job()
    assert (code, job.state, job.blocked_reason) == (rn.EXIT_BLOCKED, "BLOCKED", reason), out
    assert job.blocked_step == step
    report = world.report()
    assert f"**BLOCKED: {reason}**" in report
    assert rn.BLOCKED_HELP[reason] in report


def test_every_blocked_reason_has_report_wording():
    from gaffer import store as st

    assert set(st.BLOCKED_REASONS) <= set(rn.BLOCKED_HELP)


def test_writer_blocked_prints_the_question(world):
    _writer_blocked(world)
    _, out = world.run()
    assert "Should feature() be async?" in out


def test_dirty_writer_gets_exactly_one_nudge(world):
    _writer_dirty(world)
    world.run()
    assert [c.kind for c in world.children.calls] == ["launch", "resume"]
    assert world.children.calls[1].prompt == ch.NUDGE_PROMPT
    assert world.children.calls[1].session_id == world.children.calls[0].session_id


def test_a_nudge_that_commits_passes(world):
    def dirty(call: WriterCall):
        (call.clone / "src/app/__init__.py").write_text(GOOD_SOURCE)
        return {"status": "done", "summary": "forgot", "question": None}

    def commits(call: WriterCall):
        run_git(call.clone, "add", "-A")
        run_git(call.clone, "commit", "-q", "-m", "commit it")
        return {"status": "done", "summary": "committed", "question": None}

    world.children.writer_actions = [dirty, commits]
    assert world.run()[0] == 0


def test_tester_retry_carries_the_error_then_succeeds(world):
    bad = json.loads(json.dumps(TESTER_OUTPUT))
    bad["files"][0]["path"] = "src/evil.py"
    world.children.tester_outputs = [bad, TESTER_OUTPUT]
    assert world.run()[0] == 0
    assert world.children.tester_calls == 2
    # Only the valid tester commit exists, as the branch's first commit.
    assert branch_log(world).count("ENG-1: tester tests") == 1


def test_verify_failed_publishes_nothing(world):
    _verify_never_passes(world)
    world.run()
    job = world.job()
    assert world.remote_head() is None and not world.gh.pulls
    assert job.cycles_used == 3 and job.cycle_limit == 3


def test_review_timeout_counts_supervised_seconds(world):
    _never_concludes(world)
    world.run()
    with world.store() as s:
        rnd = s.rounds(world.job().id)[-1]
    assert rnd.outcome == "timed_out" and rnd.supervised_seconds >= world.repo.round_timeout


def test_secret_detected_never_reaches_the_remote(world):
    _secret(world)
    world.run()
    assert world.remote_head() is None
    assert "FAKESECRET_ABCDEFGHIJKL" not in world.report()


def test_closed_pr_exits_3(world):
    def hook(gh, n):
        gh.pr()["state"] = "closed"
        gh.pr()["merged"] = True

    world.gh.on_poll = hook
    code, out = world.run()
    assert code == rn.EXIT_CLOSED and world.job().state == "CLOSED"
    assert "merged by someone else" in world.report()


def test_state_lost_when_a_gaffer_branch_exists_without_a_job(world):
    run_git(world.remote, "branch", "gaffer/ENG-1", "main")
    code, out = world.run()
    job = world.job()
    assert (code, job.blocked_reason) == (rn.EXIT_BLOCKED, "state_lost")
    assert world.linear.calls == []


def test_unknown_reason_from_workspace_is_storable(world):
    """ADAPTER: workspace's extra reason codes are accepted by store.block()."""
    from gaffer import store as st

    for r in rn.RUNNER_BLOCKED_REASONS:
        assert r in st.BLOCKED_REASONS


# --- local repair loop (R24) -------------------------------------------------------


def test_local_repair_loop_counts_a_cycle_and_never_publishes_the_failing_head(world):
    world.children.writer_actions = [implement_bad, address_fix]
    code, out = world.run()
    assert code == 0, out
    job = world.job()
    assert job.cycles_used == 1
    with world.store() as s:
        packets = s.packets(job.id)
    assert [p.kind for p in packets] == ["local"]
    assert [c.kind for c in world.children.calls] == ["launch", "resume"]
    assert world.children.calls[1].session_id == world.children.calls[0].session_id
    assert "tests/test_feature.py::tests.test_feature::test_feature" in world.children.calls[1].prompt
    pushed = [e.detail["head"] for e in world.events("push")]
    assert len(pushed) == 1 and pushed[0] == job.head_sha
    assert world.gh.round_comments() and len(world.gh.round_comments()) == 1  # local packets have no round comment


def test_a_regression_outside_the_tester_tests_fails_verify(world):
    def breaks(call: WriterCall):
        commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE, "src/app/broken.py": "x\n"})
        return {"status": "done", "summary": "x", "question": None}

    def repairs(call: WriterCall):
        sha = commit(call.clone, {"src/app/broken.py": None}, "remove broken")
        return {"status": "done", "summary": "fixed", "question": None,
                "dispositions": [{"item_id": i, "disposition": "fixed", "commit_sha": sha, "reason": "x"} for i in call.item_ids]}

    world.children.writer_actions = [breaks, repairs]
    assert world.run()[0] == 0
    assert "test_regression" in world.children.calls[1].prompt


# --- rounds and packets ---------------------------------------------------------------


def test_feedback_round_resumes_the_same_session_once_and_comments_once_per_round(world):
    def hook(gh, n):
        if n == 1:
            gh.add_inline("Handle feature() being called twice.")

    world.gh.on_poll = hook
    code, out = world.run()
    assert code == 0, out
    calls = world.children.calls
    assert [c.kind for c in calls] == ["launch", "resume"]
    assert calls[1].session_id == calls[0].session_id
    assert "Handle feature() being called twice." in calls[1].prompt
    job = world.job()
    assert job.cycles_used == 1
    comments = world.gh.round_comments()
    markers = [l for c in comments for l in c["body"].splitlines() if l.startswith("gaffer-round: ")]
    with world.store() as s:
        rounds = s.rounds(job.id)
    assert markers == [f"gaffer-round: ENG-1/{r.head_sha}/{r.seq}" for r in rounds]
    assert len(set(markers)) == len(markers) == 2
    assert "fixed in" in comments[0]["body"]
    assert "fixed in `" in world.report()


def test_mid_round_comment_waits_for_round_completion(world):
    """SC6: a comment arriving mid-round triggers no resume until the round completes."""
    def hook(gh, n):
        if n == 1:
            gh.add_inline("first")
        if n == 2:
            gh.add_inline("second, mid-settle")

    world.gh.on_poll = hook
    assert world.run()[0] == 0
    resumes = [c for c in world.children.calls if c.kind == "resume"]
    assert len(resumes) == 1
    assert "first" in resumes[0].prompt and "second, mid-settle" in resumes[0].prompt


def test_declined_items_close_the_round_and_a_late_comment_opens_seq2_on_the_same_head(world):
    state = {"declined": False}

    def hook(gh, n):
        if n == 1:
            gh.add_inline("questionable finding")
        if state["declined"] and not state.get("late"):
            state["late"] = True
            gh.add_issue_comment(gh.pr()["number"], "Mark: please also add a docstring.")

    def decline(call: WriterCall):
        state["declined"] = True
        return address_decline(call)

    world.gh.on_poll = hook
    world.children.writer_actions = [implement_good, decline, address_fix]
    code, out = world.run()
    assert code == 0, out
    with world.store() as s:
        job = s.job_for_issue("ENG-1")
        rounds = s.rounds(job.id)
        items = s.items(job.id)
    first_head = rounds[0].head_sha
    assert [(r.head_sha == first_head, r.seq) for r in rounds[:2]] == [(True, 1), (True, 2)]
    resumes = [c for c in world.children.calls if c.kind == "resume"]
    assert "questionable finding" in resumes[0].prompt
    assert "questionable finding" not in resumes[1].prompt  # never re-applied
    assert "docstring" in resumes[1].prompt
    declined = [i for i in items if i.disposition == "declined"]
    assert declined and declined[0].decline_reason == "not a real problem"


def test_codex_findings_are_packet_items(world):
    world.children.reviews = [{"summary": "one issue", "harness_judgements": [], "findings": [
        {"id": "F1", "severity": "major", "path": "src/app/__init__.py", "line": 5, "title": "Off by one",
         "body": "feature() should return 42 for every call."}]}]
    assert world.run()[0] == 0
    resume = [c for c in world.children.calls if c.kind == "resume"][0]
    assert "(from codex at src/app/__init__.py:5)" in resume.prompt
    assert "Off by one" in resume.prompt


def test_failed_check_becomes_a_packet_item_with_the_log_tail(world):
    fails = {"n": 0}

    def runs(sha):
        if sha == heads[0]:
            return [{"id": 99, "name": "test", "status": "completed", "conclusion": "failure", "app": {"slug": "github-actions"},
                     "output": {"title": "1 failed", "summary": "boom", "annotations_count": 0}}]
        return world.gh.default_check_runs(sha)

    heads: list[str] = []

    def hook(gh, n):
        if not heads:
            heads.append(gh.remote_sha("gaffer/ENG-1"))

    world.gh.on_poll = hook
    world.gh.check_runs_for = runs
    world.gh.logs[99] = b"\x1b[31mE   assert 1 == 2\x1b[0m\nFAILED tests/test_app.py::test_add\n"
    assert world.run()[0] == 0
    resume = [c for c in world.children.calls if c.kind == "resume"][0]
    assert "FAILED tests/test_app.py::test_add" in resume.prompt and "\x1b[31m" not in resume.prompt


def test_coderabbit_unverified_is_ready_and_says_why(world):
    world.gh.statuses = lambda sha: [{"id": 2, "context": "CodeRabbit", "state": "success", "description": "Review paused",
                                      "creator": {"login": "coderabbitai[bot]"}, "created_at": "2026-10-22T00:00:00Z"}]
    assert world.run()[0] == 0
    report = world.report()
    assert "CodeRabbit on the head: **unverified**: CodeRabbit will not review this head: status 'Review paused'" in report
    assert "unverified" in world.gh.round_comments()[0]["body"]


def test_github_errors_during_polling_never_complete_a_round(world):
    world.gh.fail_reads = 3
    assert world.run()[0] == 0
    assert len(world.events("poll_error")) >= 1


def test_own_round_comment_is_never_feedback(world):
    """R27: gaffer's round comment carries the operator's login but isn't admitted."""
    world.gh.on_poll = lambda gh, n: gh.add_inline("fix this") if n == 1 else None
    assert world.run()[0] == 0
    assert len([c for c in world.children.calls if c.kind == "resume"]) == 1


# --- R28-R31 report items ----------------------------------------------------------------


def test_newly_synced_plugin_is_blocked_and_the_writer_relaunched_once_with_a_fresh_session(world):
    world.children.init_failures = [{**INIT, "plugins": [{"name": "shiny", "source": "shiny@synced"}]}]
    code, out = world.run()
    assert code == 0, out
    launches = [c for c in world.children.calls if c.kind == "launch"]
    assert len(launches) == 2
    assert launches[0].session_id != launches[1].session_id
    assert "shiny@synced" in launches[1].plugin_keys
    assert world.job().writer_session_id == launches[1].session_id
    blocklist = json.loads((world.home / ".gaffer" / rn.PLUGIN_BLOCKLIST).read_text())
    assert blocklist == {"plugins": ["shiny@synced"]}
    assert "shiny@synced" in world.report()


def test_block_list_persists_across_jobs(world, tmp_path):
    world.children.init_failures = [{**INIT, "plugins": [{"name": "shiny", "source": "shiny@synced"}]}]
    world.run()
    world.linear.issues["ENG-2"] = rn.Issue("ENG-2", "Two", "## Acceptance criteria\n- feature() returns 42\n")
    world.run("ENG-2")
    assert "shiny@synced" in [c for c in world.children.calls if c.kind == "launch"][-1].plugin_keys


def test_ci_workflow_changes_are_listed_prominently(world):
    def act(call: WriterCall):
        commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE, ".github/workflows/ci.yml": "name: ci\non: push\n"},
               "feature\n\nTester-Change-Reason: run CI on push too\n")
        return {"status": "done", "summary": "x", "question": None}

    world.children.writer_actions = [act]
    assert world.run()[0] == 0
    report = world.report()
    assert report.index("CI workflow changes (R30)") < report.index("## gaffer's test run")
    assert "`.github/workflows/ci.yml`" in report and "run CI on push too" in report


def test_git_config_restore_is_reported(world):
    def act(call: WriterCall):
        commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE})
        run_git(call.clone, "config", "core.hooksPath", ".husky")
        return {"status": "done", "summary": "x", "question": None}

    world.children.writer_actions = [act]
    assert world.run()[0] == 0
    assert "Job clone `.git/config` restored" in world.report()
    assert "hooksPath" not in (world.clone() / ".git" / "config").read_text()


def test_php_report_lists_the_github_hosts_and_no_ci_stamp(world, tmp_path):
    world.run()
    php = make_config(tmp_path / "php", world.src, ecosystems='["php"]', runner="phpunit")
    with world.store() as s:
        text = rn.build_report(s, php, s.job_for_issue("ENG-1"))
    assert "GitHub hosts open to sandboxed code (PHP, R29)" in text
    assert "api.github.com, codeload.github.com" in text
    no_ci = make_config(tmp_path / "noci", world.src, extra="")
    assert "NO CI EVIDENCE" not in text


def test_no_ci_evidence_stamp(tmp_path):
    w = World(tmp_path)
    cfg_text = w.config.path.read_text().replace('required_checks = ["test"]', "allow_no_ci = true")
    w.config.path.write_text(cfg_text)
    w.config = gconfig.load_config(w.config.path, home=w.home)
    w.repo = w.config.repos["ENG"]
    w.gh.check_runs_for = lambda sha: []
    assert w.run()[0] == 0
    assert "NO CI EVIDENCE" in w.report()


# --- recovery table (SC4) -----------------------------------------------------------------


def _crash_in_tester(w: World):
    w.children.tester_outputs = [Crash]


def _crash_in_red_first(w: World):
    w.children.crash_after_test_runs = 0


def _crash_in_writer(w: World):
    def act(call: WriterCall):
        commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE})
        raise Crash()

    w.children.writer_actions = [act]


def _crash_before_pr(w: World):
    orig = w.gh.__class__.__call__
    state = {"done": False}

    def transport(method, url, headers, body, timeout):
        if method == "POST" and url.endswith("/pulls") and not state["done"]:
            state["done"] = True
            raise Crash()
        return orig(w.gh, method, url, headers, body, timeout)

    _patch_transport(w, transport)


def _crash_after_pr(w: World):
    orig = w.gh.__class__.__call__
    state = {"done": False}

    def transport(method, url, headers, body, timeout):
        resp = orig(w.gh, method, url, headers, body, timeout)
        if method == "POST" and url.endswith("/pulls") and not state["done"]:
            state["done"] = True
            raise Crash()
        return resp

    _patch_transport(w, transport)


def _patch_transport(w: World, transport):
    real = w.deps

    def deps(store, out):
        d = real(store, out)
        d.github = lambda repo: rn.GitHubPorts(ws_mod.GitHubClient(w.gh.github, Secret("ghp_fake"), transport=transport), w.gh)
        return d

    w.deps = deps  # type: ignore[method-assign]


def _crash_in_round(w: World):
    def hook(gh, n):
        if n == 1:
            raise Crash()

    w.gh.on_poll = hook


def _crash_in_resume(w: World):
    w.gh.on_poll = lambda gh, n: gh.add_inline("please fix") if n == 1 else None

    def act(call: WriterCall):
        commit(call.clone, {"src/app/partial.py": "x = 1\n"}, "partial fix")
        raise Crash()

    w.children.writer_actions = [implement_good, act]


def _crash_after_resume_result(w: World):
    """The packet result is recorded; gaffer dies posting the round comment."""
    w.gh.on_poll = lambda gh, n: gh.add_inline("please fix") if n == 1 else None
    orig = w.gh.__class__.__call__
    state = {"done": False}

    def transport(method, url, headers, body, timeout):
        resp = orig(w.gh, method, url, headers, body, timeout)
        if method == "POST" and "/comments" in url and not state["done"]:
            state["done"] = True
            raise Crash()
        return resp

    _patch_transport(w, transport)


def _check_tester_rerun(w: World):
    assert w.children.tester_calls == 2
    assert branch_log(w).count("ENG-1: tester tests") == 1


def _check_tester_kept(w: World):
    assert w.children.tester_calls == 2  # the crashed red-first left no recorded tester commit: rerun
    assert branch_log(w).count("ENG-1: tester tests") == 1


def _check_writer_continued(w: World):
    calls = w.children.calls
    assert [c.kind for c in calls] == ["launch", "resume"]
    assert calls[1].session_id == calls[0].session_id and calls[1].prompt == ch.CONTINUE_PROMPT
    assert w.children.tester_calls == 1  # tester commit on branch: kept


def _check_one_pr(w: World):
    assert len(w.gh.pulls) == 1
    assert [e.detail["pushed"] for e in w.events("push")] == [True, False]  # remote head equals local: push skipped
    assert w.children.reviewer_calls == 1


def _check_round_resumed(w: World):
    assert w.children.reviewer_calls == 1  # recorded Codex review for the head: reused
    with w.store() as s:
        rounds = s.rounds(s.job_for_issue("ENG-1").id)
    assert [(r.seq, r.outcome) for r in rounds] == [(1, "complete")]
    assert len(w.gh.round_comments()) == 1


def _check_packet_resumed(w: World):
    resumes = [c for c in w.children.calls if c.kind == "resume"]
    unfence = lambda t: re.sub(r"UNTRUSTED (\w+) [0-9a-f]{16}", r"UNTRUSTED \1", t)  # noqa: E731
    assert len(resumes) == 2 and unfence(resumes[0].prompt) == unfence(resumes[1].prompt)  # the same packet
    with w.store() as s:
        job = s.job_for_issue("ENG-1")
        assert len(s.packets(job.id)) == 1
    assert job.cycles_used == 1


def _check_result_not_resumed_again(w: World):
    assert len([c for c in w.children.calls if c.kind == "resume"]) == 1
    markers = [l for c in w.gh.round_comments() for l in c["body"].splitlines() if l.startswith("gaffer-round: ")]
    assert len(markers) == len(set(markers)) == 2


RECOVERY = [
    ("TESTER", _crash_in_tester, _check_tester_rerun),
    ("RED_FIRST", _crash_in_red_first, _check_tester_kept),
    ("WRITER (child dead)", _crash_in_writer, _check_writer_continued),
    ("PUBLISH (before PR)", _crash_before_pr, _check_one_pr),
    ("PUBLISH (after PR)", _crash_after_pr, _check_one_pr),
    ("ROUND", _crash_in_round, _check_round_resumed),
    ("RESUME (child dead)", _crash_in_resume, _check_packet_resumed),
    ("RESUME (result recorded)", _crash_after_resume_result, _check_result_not_resumed_again),
]


@pytest.mark.parametrize("row, crash, check", RECOVERY, ids=[r[0] for r in RECOVERY])
def test_recovery_table(world, row, crash, check):
    crash(world)
    code, out = world.run()
    assert code == "crash", out
    code, out = world.run()
    assert code == rn.EXIT_READY, out
    check(world)
    assert len(world.gh.pulls) == 1  # never a second PR


# --- Linear (D1) and acceptance criteria -------------------------------------------------


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_linear_client_sends_one_read_only_query_with_the_key():
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return _Resp(json.dumps({"data": {"issue": {"identifier": "ENG-7", "title": "T", "description": "D",
                                                     "url": "https://linear.app/x/ENG-7"}}}).encode())

    issue = rn.LinearClient(Secret("lin_api_" + "x" * 20), opener=opener).fetch_issue("ENG-7")
    assert issue == rn.Issue("ENG-7", "T", "D", "https://linear.app/x/ENG-7")
    (req,) = seen
    body = json.loads(req.data)
    assert req.get_method() == "POST" and req.full_url == rn.LINEAR_API
    assert "mutation" not in body["query"] and body["variables"] == {"id": "ENG-7"}
    assert req.unredirected_hdrs["Authorization"] == "lin_api_" + "x" * 20


@pytest.mark.parametrize("payload, needle", [
    ({"errors": [{"message": "auth"}]}, "refused the query"),
    ({"data": {"issue": None}}, "has no issue"),
    ({"data": {"issue": {"identifier": "ENG-8", "title": "", "description": ""}}}, "answered with"),
])
def test_linear_client_errors(payload, needle):
    client = rn.LinearClient(Secret("k"), opener=lambda req, t: _Resp(json.dumps(payload).encode()))
    with pytest.raises(rn.LinearError, match=needle):
        client.fetch_issue("ENG-7")


def test_linear_token_comes_from_the_keychain():
    class KC:
        def find_generic_password(self, service, account):
            assert (service, account) == ("gaffer", "linear-token")
            return "lin_api_abc"

    assert rn.linear_token(KC()).reveal() == "lin_api_abc"

    class Empty:
        def find_generic_password(self, service, account):
            return None

    with pytest.raises(gconfig.KeychainError, match="linear-token"):
        rn.linear_token(Empty())


@pytest.mark.parametrize("text, found", [
    ("## Acceptance criteria\n- returns 42\n\n## Notes\nx", "- returns 42"),
    ("**Acceptance Criteria**\n1. a\n2. b", "1. a\n2. b"),
    ("Do it.\n\n- [ ] one\n- [x] two\n", "- [ ] one\n- [x] two"),
    ("## Acceptance criteria\n\n## Next\nstuff", None),
    ("Make it better.", None),
])
def test_acceptance_criteria(text, found):
    assert rn.acceptance_criteria(text) == found
