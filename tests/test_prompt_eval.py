"""`gaffer eval` (T11, R19): the three-ticket prompt eval and its baselines.

The runs use runner_fakes' world: real git, workspace, store and the EvalRunner state machine,
with fake children (and so no model), a fake gitleaks and a fake clock. The eval's toy is
runner_fakes' small app repo so the fake test runner's rules apply; it also reports the hidden
tests. The real evals/ fixtures are checked separately with the real pytest.
"""

from __future__ import annotations

import dataclasses
import fcntl
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from gaffer import children as ch
from gaffer import prompt_eval as pe
from gaffer import runner as rn
from gaffer import store as st

import runner_fakes as rf
from runner_fakes import World

HIDDEN = '''from app import feature


def test_feature_hidden():
    assert feature() == 42


def test_feedback_hidden():
    pass
'''

TICKET = '''key = "{key}"
kind = "feature_missing_symbol"
title = "Add feature"
description = """
Add feature().

## Acceptance criteria

- [ ] feature() returns 42
"""

[feedback]
source = "coderabbit"
path = "src/app/__init__.py"
line = 5
body = "SEEDED-FEEDBACK-{key}: feature() needs a docstring."

[expected]
red_first = "missing_symbol"
address = ["fixed"]
'''


class EvalChildren(rf.FakeChildren):
    """FakeChildren whose test run also reports the hidden tests when gaffer adds them:
    test_feedback_* passes once a fix commit (address_fix's src/app/fix_*.py) exists,
    every other one when the feature returns 42. Its doctor proves the config eval hands it."""

    doctor_cfg = None

    def doctor(self):
        report = super().doctor()
        return dataclasses.replace(report, deny_paths=rf.doctor_mod.deny_list(self.doctor_cfg))

    def run_test_command(self, repo, jobs, cwd, *, command, on_spawn):
        out = super().run_test_command(repo, jobs, cwd, command=command, on_spawn=on_spawn)
        hidden = Path(cwd) / pe.HIDDEN_TEST_PATH
        if hidden.exists():
            src = (Path(cwd) / "src/app/__init__.py").read_text()
            fixed = any((Path(cwd) / "src/app").glob("fix_*.py"))
            cases = ""
            for name in re.findall(r"^def (test_\w+)", hidden.read_text(), re.M):
                ok = fixed if "feedback" in name else "return 42" in src
                cases += (f'<testcase classname="tests.test_eval_hidden" name="{name}" time="0.0"'
                          + (" />" if ok else '><failure message="no">E   AssertionError: no</failure></testcase>'))
            junit = Path(cwd) / repo.junit_path
            junit.write_text(junit.read_text().replace("</testsuite>", cases + "</testsuite>"))
        return out


class EvalWorld(World):
    def __init__(self, tmp: Path, *, tickets: tuple[str, ...] = ("EVAL-1", "EVAL-2")):
        super().__init__(tmp)
        self.children = EvalChildren(self)
        self.evals = tmp / "evals"
        shutil.copytree(self.src, self.evals / "toy", ignore=shutil.ignore_patterns(".git"))
        for key in tickets:
            d = self.evals / "tickets" / key
            d.mkdir(parents=True)
            (d / "ticket.toml").write_text(TICKET.format(key=key))
            (d / "hidden_test.py").write_text(HIDDEN)
        self.factory_calls: list[tuple[object, object]] = []

    def eval(self, *, save_baseline: bool = False, promote: str | None = None, config: Path | None = None) -> tuple[int, str]:
        out = io.StringIO()

        def children(cfg, doctor_cfg):
            self.factory_calls.append((cfg, doctor_cfg))
            self.children.doctor_cfg = doctor_cfg
            return self.children

        self.clock.sleep(1)  # each run gets its own timestamped run id

        code = pe.eval_main(config, home=self.home, save_baseline=save_baseline, promote=promote, out=out,
                            evals_dir=self.evals, children=children, scanner=self.scanner, clock=self.clock,
                            scratch_dir=self.tmp)
        text = out.getvalue()
        self.outputs.append(text)
        return code, text

    def baseline(self) -> dict:
        return json.loads(pe.baseline_path(self.home).read_text())

    def runs(self) -> list[dict]:
        d = pe.evals_home(self.home) / "runs"
        return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.exists() else []


@pytest.fixture
def world(tmp_path):
    return EvalWorld(tmp_path)


def _ticket(record: dict, key: str) -> dict:
    return next(t for t in record["tickets"] if t["key"] == key)


# --- the first run stores the baseline ------------------------------------------------------


def test_first_run_stores_baseline(world):
    code, out = world.eval()
    assert code == pe.EXIT_PASS, out
    assert "stored run" in out and "as the baseline" in out
    base = world.baseline()
    runs = world.runs()
    assert len(runs) == 1 and base["run_id"] == runs[0]["run_id"] and base["complete"] is True
    assert base["promoted_at"]
    assert base["prompt_hashes"] == pe.prompt_hashes() and set(base["prompt_hashes"]) == {
        "tester.md", "implement.md", "review.md", "address.md"}
    assert base["cli"] == {"claude": "2.1.304 (Claude Code)", "codex": "codex-cli 0.160.0"}
    assert base["writer_model"] == "opus"
    assert [t["key"] for t in base["tickets"]] == ["EVAL-1", "EVAL-2"]
    for t in base["tickets"]:
        assert t["final"] == "done"
        assert t["stages"] == {"tester": "ok", "red_first": "valid", "writer": "ok", "verify": "passed", "review": "ok",
                               "address": "fixed", "verify_after_address": "passed", "hidden_tests": "passed"}
        assert t["red_first_valid"] is True and t["final_tests_green"] is True
        assert t["red_first_verdicts"] and all(v["verdict"] == "valid_collection_error" for v in t["red_first_verdicts"])
        assert t["review_findings"] == 0 and t["address"] == "fixed"
        assert t["hidden"] == {"passed": 2, "total": 2, "failed": [], "error": None}
        assert t["expected"] == {"completed": True, "red_first_missing_symbol": True, "address_fixed": True,
                                 "hidden_tests_pass": True}
        assert t["metrics"]["writer_turns"] == 6  # implement + address, 3 turns each (fake)
        assert t["metrics"]["repair_cycles"] == 0
        assert t["tests"] == {"acceptance": 1, "guard": 0, "untestable": 0}
    # Each ticket: one tester, one Codex review, a launch and one resume carrying the seeded item.
    assert world.children.tester_calls == 2 and world.children.reviewer_calls == 2
    resumes = [c for c in world.children.calls if c.kind == "resume"]
    assert [c.kind for c in world.children.calls] == ["launch", "resume", "launch", "resume"]
    assert "SEEDED-FEEDBACK-EVAL-1" in resumes[0].prompt and "SEEDED-FEEDBACK-EVAL-2" in resumes[1].prompt
    assert "repair cycle 1 of 3" in resumes[0].prompt  # the address.md template, rendered by children
    # The doctor gate ran once (no pass recorded yet) and recorded into gaffer's real state.db.
    assert world.children.doctor_calls == 1
    with world.store() as s:
        assert s.last_doctor_pass() is not None


def test_eval_scratch_lives_under_gaffer_evals(world):
    code, _ = world.eval()
    assert code == 0
    run_id = world.baseline()["run_id"]
    run_dir = pe.evals_home(world.home) / "runs" / run_id
    assert (run_dir / "EVAL-1.db").exists() and (run_dir / "EVAL-1.log").exists()
    assert (run_dir / "jobs" / "EVAL-1" / "report.md").exists()  # Deps.state_root, not ~/.gaffer/jobs
    assert not (world.home / ".gaffer" / "jobs" / "EVAL-1").exists()
    # gaffer's real store holds no eval job: eval jobs never block `gaffer run` (R31 active-job refusal).
    with world.store() as s:
        assert s.job_for_issue("EVAL-1") is None and s.active_job() is None
    cfg, doctor_cfg = world.factory_calls[0]
    assert set(cfg.repos) == {"EVAL"} and doctor_cfg == world.config  # doctor proves the user's config


def test_unchanged_second_run_passes(world):
    assert world.eval()[0] == 0
    first = world.baseline()["run_id"]
    code, out = world.eval()
    assert code == pe.EXIT_PASS, out
    assert f"compared with baseline {first}" in out
    assert "prompts changed since the baseline: none" in out
    assert "eval fixtures changed since the baseline: none" in out
    assert "0 regression(s), 0 warning(s)" in out
    assert world.baseline()["run_id"] == first  # not promoted without --save-baseline
    assert len(world.runs()) == 2
    assert world.children.doctor_calls == 1  # the pass still holds: no second doctor run


# --- regressions ---------------------------------------------------------------------------


def _regress_decline(w):
    w.children.default_address = rf.address_decline


def _still_bad(call):
    """A repair that commits something but leaves feature() wrong."""
    sha = rf.commit(call.clone, {f"src/app/try_{time.time_ns()}.py": "x = 1\n"}, "try again")
    return {"status": "done", "summary": "tried", "question": None,
            "dispositions": [{"item_id": i, "disposition": "fixed", "commit_sha": sha, "reason": "tried"}
                             for i in call.item_ids or []]}


def _regress_verify(w):
    w.children.default_implement = rf.implement_bad
    w.children.default_address = _still_bad  # every repair stays wrong: verify_failed after 3 cycles


def _regress_tester(w):
    w.children.tester_outputs = [{"criteria": []}, {"criteria": []}] * 2


def _regress_review(w):
    w.children.reviews = ["error", "error"]


def _regress_already_passing(w):
    # The fake runner passes every tests/test_extra_*.py on any code: an acceptance test green on base.
    out = json.loads(json.dumps(rf.TESTER_OUTPUT))
    out["tests"][0].update(file="tests/test_extra_one.py", classname="tests.test_extra_one", name="test_it")
    out["files"] = [{"path": "tests/test_extra_one.py", "content": "def test_it():\n    pass\n"}]
    out["expected_missing"] = []
    w.children.tester_outputs = [out]


@pytest.mark.parametrize("mutate, expected", [
    (_regress_decline, ["EVAL-1: hidden tests 2/2 -> 1/2 (failing: test_feedback_hidden)",
                        "EVAL-1 hidden_tests: passed -> failed",
                        "EVAL-1: expected outcome address_fixed met in the baseline, not now"]),
    (_regress_verify, ["EVAL-1: completed in the baseline, now blocked: verify_failed at VERIFY",
                       "EVAL-1 verify: passed -> verify_failed", "EVAL-1: final tests green in the baseline, not now"]),
    (_regress_tester, ["EVAL-1 tester: ok -> tester_invalid", "EVAL-1: red-first valid in the baseline, not now"]),
    (_regress_review, ["EVAL-1 review: ok -> reviewer_failed"]),
    (_regress_already_passing, ["EVAL-1 red_first: valid -> already_passing"]),
], ids=["address", "verify", "tester", "review", "red_first"])
def test_compare_detects_each_regression_kind(world, mutate, expected):
    assert world.eval()[0] == 0
    mutate(world)
    code, out = world.eval()
    assert code == pe.EXIT_REGRESSION, out
    for line in expected:
        assert f"REGRESSION: {line}" in out, (line, out)
    assert "gaffer eval: REGRESSION against baseline" in out and "--promote" in out
    run = world.runs()[-1]
    assert run["complete"] is True  # a regression is still a complete run
    assert world.baseline()["run_id"] != run["run_id"]


def test_a_tester_retry_is_a_warning_not_a_regression(world):
    assert world.eval()[0] == 0
    # A test the run never reports, in a file that collects fine: an invalid red, retried once (R5).
    typo = json.loads(json.dumps(rf.TESTER_OUTPUT))
    typo["tests"][0].update(file="tests/test_extra_typo.py", classname="tests.test_extra_typo", name="test_wrong")
    typo["files"] = [{"path": "tests/test_extra_typo.py", "content": "def test_it():\n    pass\n"}]
    world.children.tester_outputs = [typo, rf.TESTER_OUTPUT]
    code, out = world.eval()
    assert code == pe.EXIT_PASS, out
    assert "warning: EVAL-1 tester: ok -> ok_after_retry" in out
    assert "warning: EVAL-1 red_first: valid -> valid_after_retry" in out
    assert "0 regression(s), 2 warning(s)" in out
    assert world.runs()[-1]["tickets"][0]["metrics"]["tester_attempts"] == 2


def test_declined_feedback_is_a_change_not_a_regression_by_itself(world):
    assert world.eval()[0] == 0
    _regress_decline(world)
    _, out = world.eval()
    assert "changed: EVAL-1 address: fixed -> declined" in out
    assert "REGRESSION: EVAL-1 address" not in out


def test_regression_run_promoted_with_promote(world):
    assert world.eval()[0] == 0
    _regress_decline(world)
    code, _ = world.eval()
    assert code == pe.EXIT_REGRESSION
    run_id = world.runs()[-1]["run_id"]
    assert run_id != world.baseline()["run_id"]
    tester_calls = world.children.tester_calls
    code, out = world.eval(promote=run_id)
    assert code == 0 and f"run {run_id} is now the baseline" in out
    assert world.baseline()["run_id"] == run_id
    assert world.children.tester_calls == tester_calls  # promoting runs no child


def test_save_baseline_promotes_this_run(world):
    assert world.eval()[0] == 0
    _regress_decline(world)
    code, out = world.eval(save_baseline=True)
    assert code == pe.EXIT_PASS
    assert "REGRESSION: EVAL-1: hidden tests 2/2 -> 1/2" in out  # the comparison is still shown
    assert "replacing" in out
    assert world.baseline()["tickets"][0]["address"] == "declined"


def test_promote_refuses_unknown_or_incomplete_runs(world):
    assert world.eval(promote="not-a-run")[0] == pe.EXIT_REFUSED
    assert world.eval(promote="20261023T000000-abcdef")[0] == pe.EXIT_REFUSED
    world.children.tester_outputs = [ch.LimitEvent("codex", "usage limit reached", None)]
    assert world.eval()[0] == pe.EXIT_INCOMPLETE
    run_id = world.runs()[-1]["run_id"]
    code, out = world.eval(promote=run_id)
    assert code == pe.EXIT_REFUSED and "incomplete" in out
    assert not pe.baseline_path(world.home).exists()


def test_prompt_change_is_reported(world, tmp_path, monkeypatch):
    assert world.eval()[0] == 0
    prompts = tmp_path / "prompts"
    shutil.copytree(ch.PROMPTS_DIR, prompts)
    with (prompts / "implement.md").open("a") as f:
        f.write("\nA reworded line.\n")
    monkeypatch.setattr(ch, "PROMPTS_DIR", prompts)
    code, out = world.eval()
    assert code == pe.EXIT_PASS, out
    assert "prompts changed since the baseline: implement.md" in out
    assert "A reworded line." in world.children.calls[-2].prompt  # the run used the changed prompt
    assert world.runs()[-1]["prompt_hashes"]["implement.md"] != world.baseline()["prompt_hashes"]["implement.md"]


# --- refusals, limits and the network ---------------------------------------------------------


def test_refuses_on_failing_doctor(world):
    world.children.doctor_failures = {"claude_file_read": "Read of ~/.ssh succeeded"}
    code, out = world.eval()
    assert code == pe.EXIT_REFUSED, out
    assert "refused: doctor failed (claude_file_read; R18)" in out
    assert world.children.doctor_calls == 1
    assert world.children.tester_calls == 0 and world.children.calls == []
    assert not pe.baseline_path(world.home).exists() and world.runs() == []
    with world.store() as s:
        assert s.last_doctor_pass() is None  # recorded as a failure, never a pass


def test_stale_doctor_reruns_like_gaffer_run(world):
    assert world.eval()[0] == 0
    world.children.versions = ("2.1.400 (Claude Code)", "codex-cli 0.160.0")
    world.children.doctor_failures = {"codex_read": "~/.ssh readable"}
    code, out = world.eval()
    assert code == pe.EXIT_REFUSED
    assert "running doctor (R18): Claude Code 2.1.304 (Claude Code) -> 2.1.400 (Claude Code)" in out
    assert "FAIL  codex_read" in out  # the doctor report reaches the console, not just the log
    assert world.children.doctor_calls == 2


def test_refuses_when_preflight_fails(world):
    world.children.preflight_error = ["ANTHROPIC_API_KEY is set"]
    code, out = world.eval()
    assert code == pe.EXIT_REFUSED and "ANTHROPIC_API_KEY" in out
    assert world.children.tester_calls == 0


def test_refuses_while_run_lock_held(world):
    path = rn.run_lock_path(world.home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out = world.eval()
    finally:
        os.close(fd)
    assert code == pe.EXIT_REFUSED and "run lock" in out
    assert world.children.tester_calls == 0


def test_usage_limit_stops_the_run_incomplete(world):
    world.children.tester_outputs = [ch.LimitEvent("codex", "usage limit reached", None)]
    code, out = world.eval()
    assert code == pe.EXIT_INCOMPLETE, out
    run = world.runs()[-1]
    assert run["complete"] is False and run["stopped"].startswith("usage limit on EVAL-1")
    assert [t["key"] for t in run["tickets"]] == ["EVAL-1"]  # EVAL-2 never started
    assert run["tickets"][0]["final"] == "blocked: usage_limit at TESTER"
    assert run["tickets"][0]["stages"]["tester"] == "usage_limit"
    assert not pe.baseline_path(world.home).exists()
    assert world.children.tester_calls == 1


def test_incomplete_run_against_a_baseline_reports_missing_tickets(world):
    assert world.eval()[0] == 0
    world.children.tester_outputs = [ch.LimitEvent("codex", "usage limit reached", None)]
    code, out = world.eval()
    assert code == pe.EXIT_INCOMPLETE
    assert "REGRESSION: EVAL-2: not run in this run" in out
    assert "INCOMPLETE" in out


def test_no_network_github_or_linear_call(world, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access during gaffer eval")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(rn, "LinearClient", refuse)
    monkeypatch.setattr(rn.ws_mod, "GitHubClient", refuse)
    monkeypatch.setattr(rn.ws_mod, "push", refuse)
    monkeypatch.setattr(rn.ws_mod, "open_pull_request", refuse)
    monkeypatch.setattr(rn.ws_mod, "post_round_summary", refuse)
    monkeypatch.setattr(rn.gconfig, "github_token", refuse)
    monkeypatch.setattr(rn, "linear_token", refuse)
    code, out = world.eval()
    assert code == 0, out
    assert world.gh.requests == [] and world.linear.calls == []
    with pytest.raises(pe.NoNetwork):
        pe.no_github(world.repo)


def test_no_push_to_the_toy_remote(world, monkeypatch):
    remotes: list[str] = []
    real = pe.make_toy

    def spy(src, dest):
        checkout, remote = real(src, dest)
        remotes.append(str(remote))
        return checkout, remote

    monkeypatch.setattr(pe, "make_toy", spy)
    assert world.eval()[0] == 0
    refs = subprocess.run(["git", "for-each-ref", "--format=%(refname)"], cwd=remotes[0], capture_output=True,
                          text=True, check=True).stdout.split()
    assert refs == ["refs/heads/main"]


def test_cli_parses_eval():
    args = rn.build_parser().parse_args(["eval", "--save-baseline"])
    assert args.command == "eval" and args.save_baseline and args.promote is None
    args = rn.build_parser().parse_args(["eval", "--promote", "20261023T000000-abcdef", "--config", "/x.toml"])
    assert args.promote == "20261023T000000-abcdef" and args.config == Path("/x.toml")
    with pytest.raises(SystemExit) as e:
        rn.build_parser().parse_args(["eval", "--save-baseline", "--promote", "x"])
    assert e.value.code == rn.EXIT_REFUSED


def test_main_dispatches_eval(monkeypatch, tmp_path):
    seen = {}

    def fake(config, **kw):
        seen.update(kw, config=config)
        return 7

    monkeypatch.setattr(pe, "eval_main", fake)
    assert rn.main(["eval", "--promote", "R"], home=tmp_path) == 7
    assert seen == {"config": None, "home": tmp_path, "save_baseline": False, "promote": "R"}


# --- compare() on its own -------------------------------------------------------------------


def _record(**over) -> dict:
    t = {"key": "EVAL-1", "kind": "bug_fix", "final": "done",
         "stages": {"tester": "ok", "red_first": "valid", "writer": "ok", "verify": "passed", "review": "ok",
                    "address": "fixed", "verify_after_address": "passed", "hidden_tests": "passed"},
         "red_first_valid": True, "final_tests_green": True, "review_findings": 1, "address": "fixed",
         "tests": {"acceptance": 3, "guard": 1, "untestable": 0},
         "hidden": {"passed": 5, "total": 5, "failed": [], "error": None},
         "expected": {"completed": True, "hidden_tests_pass": True},
         "metrics": {"wall_seconds": 300.0, "writer_turns": 20}}
    stages = over.pop("stages", {})
    t.update(over)
    t["stages"] = {**t["stages"], **stages}
    return {"run_id": "r", "prompt_hashes": {"tester.md": "a"}, "cli": {"claude": "1", "codex": "2"},
            "writer_model": "opus", "codex_model": None, "tickets": [t]}


def test_compare_unchanged_has_nothing():
    c = pe.compare(_record(), _record())
    assert (c.regressions, c.warnings, c.changes, c.improvements, c.prompts_changed) == ([], [], [], [], [])


@pytest.mark.parametrize("over, line", [
    ({"final_tests_green": False}, "EVAL-1: final tests green in the baseline, not now"),
    ({"red_first_valid": False}, "EVAL-1: red-first valid in the baseline, not now"),
    ({"hidden": {"passed": 4, "total": 5, "failed": ["test_x"], "error": None}}, "EVAL-1: hidden tests 5/5 -> 4/5 (failing: test_x)"),
    ({"expected": {"completed": True, "hidden_tests_pass": False}}, "EVAL-1: expected outcome hidden_tests_pass met in the baseline, not now"),
    ({"final": "blocked: writer_blocked at WRITER"}, "EVAL-1: completed in the baseline, now blocked: writer_blocked at WRITER"),
    ({"stages": {"writer": "writer_dirty"}}, "EVAL-1 writer: ok -> writer_dirty"),
    ({"stages": {"verify": "verify_failed"}}, "EVAL-1 verify: passed -> verify_failed"),
    ({"stages": {"address": "not_reached"}}, "EVAL-1 address: fixed -> not_reached"),
    ({"stages": {"hidden_tests": "error"}}, "EVAL-1 hidden_tests: passed -> error"),
])
def test_compare_regressions(over, line):
    c = pe.compare(_record(), _record(**over))
    assert line in c.regressions, c.regressions


@pytest.mark.parametrize("over, line", [
    ({"stages": {"verify": "passed_after_repair"}}, "EVAL-1 verify: passed -> passed_after_repair"),
    ({"stages": {"tester": "ok_after_retry"}}, "EVAL-1 tester: ok -> ok_after_retry"),
    ({"review_findings": 3}, "EVAL-1: Codex review findings 1 -> 3"),
    ({"tests": {"acceptance": 1, "guard": 1, "untestable": 0}}, "EVAL-1: fewer acceptance tests from the tester (3 -> 1)"),
    ({"metrics": {"wall_seconds": 600.0, "writer_turns": 20}}, "EVAL-1: runs longer, 300 s -> 600 s"),
    ({"metrics": {"wall_seconds": 300.0, "writer_turns": 40}}, "EVAL-1: more writer turns, 20 -> 40"),
])
def test_compare_warnings_are_not_regressions(over, line):
    c = pe.compare(_record(), _record(**over))
    assert line in c.warnings and c.regressions == []


def test_compare_small_slowdowns_are_noise():
    c = pe.compare(_record(), _record(metrics={"wall_seconds": 400.0, "writer_turns": 23}))
    assert c.warnings == [] and c.regressions == []


def test_compare_improvements_and_changes():
    base = _record(final="blocked: tester_invalid at RED_FIRST", stages={"red_first": "tester_invalid"},
                   expected={"completed": False, "hidden_tests_pass": False})
    c = pe.compare(base, _record())
    assert "EVAL-1: now completes (baseline: blocked: tester_invalid at RED_FIRST)" in c.improvements
    assert "EVAL-1 red_first: tester_invalid -> valid" in c.improvements
    assert "EVAL-1: expected outcome completed now met" in c.improvements and c.regressions == []
    c = pe.compare(_record(), _record(address="declined", stages={"address": "declined"}))
    assert c.changes == ["EVAL-1 address: fixed -> declined"] and c.regressions == []


def test_compare_reports_prompt_cli_and_model_changes():
    new = _record(prompt_hashes={"tester.md": "b"})
    new["prompt_hashes"] = {"tester.md": "b", "address.md": "c"}
    new["cli"] = {"claude": "9", "codex": "2"}
    new["writer_model"] = "sonnet"
    c = pe.compare(_record(), new)
    assert c.prompts_changed == ["address.md", "tester.md"]
    assert "claude CLI: 1 -> 9" in c.changes and "writer_model: opus -> sonnet" in c.changes
    text = pe.format_comparison(c, {"run_id": "r", "promoted_at": "t"})
    assert "prompts changed since the baseline: address.md, tester.md" in text


@pytest.mark.parametrize("kind, verdicts, ok", [
    ("missing_symbol", ["valid_collection_error", "valid_assertion"], True),  # EVAL-3's live red-first
    ("missing_symbol", ["valid_missing_symbol"], True),
    ("missing_symbol", ["valid_assertion"], False),
    ("assertion", ["valid_assertion", "valid_assertion"], True),
    ("assertion", ["valid_collection_error"], False),
    ("assertion", ["valid_assertion", "invalid"], False),
    ("assertion", [], False),
])
def test_red_first_expectation(kind, verdicts, ok):
    assert pe.red_first_as_expected(kind, [{"test": "t", "verdict": v} for v in verdicts]) is ok


def test_fixture_change_is_reported(world):
    assert world.eval()[0] == 0
    with (world.evals / "tickets" / "EVAL-2" / "hidden_test.py").open("a") as f:
        f.write("\n\ndef test_more():\n    pass\n")
    code, out = world.eval()
    assert "eval fixtures changed since the baseline: EVAL-2" in out
    assert "prompts changed since the baseline: none" in out


def test_baseline_without_fixture_hashes_says_unknown():
    run = _record()
    run["fixture_hashes"] = {"toy": "a"}
    text = pe.format_comparison(pe.compare(_record(), run), {"run_id": "r"})
    assert "eval fixtures changed since the baseline: unknown" in text


def test_compare_flags_a_ticket_missing_from_the_run():
    run = _record()
    run["tickets"] = []
    assert "EVAL-1: not run in this run" in pe.compare(_record(), run).regressions


# --- tickets and the toy ----------------------------------------------------------------------


def test_real_tickets_load():
    tickets = pe.load_tickets(pe.EVALS_DIR / "tickets")
    assert [(t.key, t.kind) for t in tickets] == [
        ("EVAL-1", "bug_fix"), ("EVAL-2", "feature_missing_symbol"), ("EVAL-3", "dependency_add")]
    assert [t.expected.red_first for t in tickets] == ["assertion", "missing_symbol", "missing_symbol"]
    assert tickets[2].expected.dependency == "humanize"
    for t in tickets:
        assert rn.acceptance_criteria(t.issue().description)
        assert t.feedback.source == "coderabbit" and t.feedback.body
        assert len(t.hidden_tests) >= 5
        assert "hidden" not in t.description.lower()  # the hidden tests are never described to children


def _pytest(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=cwd,
                          capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def test_real_toy_materializes_and_its_tests_pass(tmp_path):
    checkout, remote = pe.make_toy(pe.EVALS_DIR / "toy", tmp_path)
    assert not (pe.EVALS_DIR / "toy" / ".git").exists()  # no nested repo in gaffer's tree
    assert subprocess.run(["git", "status", "--porcelain"], cwd=checkout, capture_output=True, text=True).stdout == ""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True).stdout
    again, _ = pe.make_toy(pe.EVALS_DIR / "toy", tmp_path / "again")
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=again, capture_output=True, text=True).stdout == head
    assert subprocess.run(["git", "rev-parse", "main"], cwd=remote, capture_output=True, text=True).stdout == head
    p = _pytest(checkout)
    assert p.returncode == 0 and "2 passed" in p.stdout, p.stdout + p.stderr


SOLUTIONS = {
    "EVAL-1": ("toy/textutil.py", "def word_count(text: str) -> int:\n    return len(text.split())\n"),
    "EVAL-2": ("toy/textutil.py", "def initials(name: str) -> str:\n"
                                  "    return ''.join(next((c for c in w if c.isalpha()), '') for w in name.split()).upper()\n"),
}


@pytest.mark.parametrize("key", ["EVAL-1", "EVAL-2", "EVAL-3"])
def test_real_hidden_tests_fail_on_base_and_pass_on_a_solution(tmp_path, key):
    ticket = next(t for t in pe.load_tickets(pe.EVALS_DIR / "tickets") if t.key == key)
    checkout, _ = pe.make_toy(pe.EVALS_DIR / "toy", tmp_path)
    (checkout / pe.HIDDEN_TEST_PATH).write_text(ticket.hidden_test)
    assert _pytest(checkout, pe.HIDDEN_TEST_PATH).returncode != 0  # meaningful: red on the unchanged toy
    if key in SOLUTIONS:  # EVAL-3 needs humanize from PyPI; the no-model sandbox smoke run covered it
        path, code = SOLUTIONS[key]
        with (checkout / path).open("a") as f:
            f.write("\n\n" + code)
        p = _pytest(checkout)
        assert p.returncode == 0, p.stdout + p.stderr


# Solutions that meet the ticket but not its seeded feedback item, so that item needs a real code
# change through address.md (R33). EVAL-2's is the code the T11 baseline writer committed.
TICKET_ONLY_SOLUTIONS = {
    "EVAL-2": ("toy/textutil.py", "def initials(name: str) -> str:\n"
                                  "    return ''.join(word[0] for word in name.split()).upper()\n"),
}


@pytest.mark.parametrize("key", sorted(TICKET_ONLY_SOLUTIONS))
def test_real_seeded_item_needs_a_code_change(tmp_path, key):
    ticket = next(t for t in pe.load_tickets(pe.EVALS_DIR / "tickets") if t.key == key)
    assert ticket.expected.address == ("fixed",)
    checkout, _ = pe.make_toy(pe.EVALS_DIR / "toy", tmp_path)
    (checkout / pe.HIDDEN_TEST_PATH).write_text(ticket.hidden_test)
    path, code = TICKET_ONLY_SOLUTIONS[key]
    with (checkout / path).open("a") as f:
        f.write("\n\n" + code)
    p = _pytest(checkout, "-rf", pe.HIDDEN_TEST_PATH)
    failed = {ln.split("::")[1].split(" ")[0] for ln in p.stdout.splitlines() if ln.startswith("FAILED")}
    feedback_tests = {n for n in ticket.hidden_tests if n.endswith("_feedback")}
    assert feedback_tests and failed == feedback_tests, p.stdout + p.stderr


@pytest.mark.parametrize("edit, problem", [
    (lambda t: t.replace('key = "EVAL-1"', 'key = "ENG-1"'), "must be EVAL-<n>"),
    (lambda t: t.replace("## Acceptance criteria", "## Notes").replace("- [ ]", "-"), "Acceptance criteria"),
    (lambda t: t.replace('source = "coderabbit"', 'source = "github"'), "feedback.source"),
    (lambda t: t.replace('red_first = "missing_symbol"', 'red_first = "typo"'), "expected.red_first"),
    (lambda t: t.replace('address = ["fixed"]', 'address = ["ignored"]'), "expected.address"),
    (lambda t: t.replace("line = 5", "line = 0"), "feedback.line"),
])
def test_bad_ticket_files_are_rejected(tmp_path, edit, problem):
    d = tmp_path / "EVAL-1"
    d.mkdir()
    (d / "ticket.toml").write_text(edit(TICKET.format(key="EVAL-1")))
    (d / "hidden_test.py").write_text(HIDDEN)
    with pytest.raises(pe.EvalError, match=re.escape(problem)):
        pe.load_tickets(tmp_path)


def test_hidden_file_without_tests_is_rejected(tmp_path):
    d = tmp_path / "EVAL-1"
    d.mkdir()
    (d / "ticket.toml").write_text(TICKET.format(key="EVAL-1"))
    (d / "hidden_test.py").write_text("x = 1\n")
    with pytest.raises(pe.EvalError, match="no test functions"):
        pe.load_tickets(tmp_path)


def test_dependency_check_reads_requirements_at_the_final_head(world):
    world.evals.joinpath("tickets/EVAL-1/ticket.toml").write_text(
        TICKET.format(key="EVAL-1").replace('address = ["fixed"]', 'address = ["fixed"]\ndependency = "humanize"'))
    shutil.rmtree(world.evals / "tickets" / "EVAL-2")
    (world.evals / "toy" / "requirements.txt").write_text("pytest\nhumanize-extra\n")  # a near miss on base

    def implement(call):
        rf.commit(call.clone, {"src/app/__init__.py": rf.GOOD_SOURCE, "requirements.txt": "pytest\nHumanize>=4\n"},
                  "implement feature\n\nTester-Change-Reason: the ticket adds humanize")
        return {"status": "done", "summary": "ok", "question": None}

    world.children.writer_actions = [implement]
    code, out = world.eval()
    assert code == 0, out
    t = world.baseline()["tickets"][0]
    assert t["expected"]["declares_humanize"] is True
    world.children.writer_actions = []  # second run: the writer forgets the manifest
    code, out = world.eval()
    assert code == pe.EXIT_REGRESSION
    assert "REGRESSION: EVAL-1: expected outcome declares_humanize met in the baseline, not now" in out


def test_defaults_mode_without_gaffer_toml(world):
    world.config.path.unlink()  # Mark has no ~/.gaffer/gaffer.toml
    code, out = world.eval()
    assert code == 0, out
    assert "running under built-in defaults" in out
    cfg, doctor_cfg = world.factory_calls[0]
    assert doctor_cfg is None  # doctor proves the defaults, as `gaffer doctor` does (T10)
    assert cfg.writer_model == "opus" and cfg.codex_model is None
    assert cfg.vault_path.is_relative_to(pe.evals_home(world.home))  # a stand-in, inside ~/.gaffer
    assert not cfg.job_root.is_relative_to(world.home / ".gaffer")
    assert world.baseline()["config"] == "defaults"
    assert world.children.doctor_calls == 1
    with world.store() as s:
        assert s.last_doctor_pass().results["deny_paths"] == list(rf.doctor_mod.deny_list(None))
    assert not world.config.path.exists()  # never writes gaffer.toml


def test_real_eval_children_run_doctor_on_the_doctor_config(world, monkeypatch):
    seen = []
    monkeypatch.setattr(rn.doctor_mod, "run_doctor", lambda cfg, *, paths: seen.append((cfg, paths)) or "report")
    eval_cfg = pe.eval_config(None, home=world.home, checkout=world.src, job_root=world.tmp / "j",
                              scratch=world.home / ".gaffer" / "evals" / "x")
    kids = pe.EvalChildren(eval_cfg, None)
    assert kids.doctor() == "report" and seen[0][0] is None  # the defaults, never the eval config
    assert pe.EvalChildren(eval_cfg, world.config).doctor() == "report" and seen[1][0] is world.config
    assert kids.cfg is eval_cfg and set(kids.cfg.repos) == {"EVAL"}


def test_codex_findings_are_counted_but_only_the_seeded_item_is_addressed(world):
    world.children.reviews = [{"summary": "two issues", "findings": [
        {"id": "F1", "severity": "major", "path": "src/app/__init__.py", "line": 5, "title": "First finding", "body": "One."},
        {"id": "F2", "severity": "minor", "path": None, "line": None, "title": "Second finding", "body": "Two."}],
        "harness_judgements": [{"path": "tests/conftest.py", "verdict": "unjustified", "reason": "Not needed."}]}]
    assert world.eval()[0] == 0
    t = _ticket(world.baseline(), "EVAL-1")
    assert t["review_findings"] == 2 and t["harness_judgements"] == {"unjustified": 1}
    assert _ticket(world.baseline(), "EVAL-2")["review_findings"] == 0
    resume = [c for c in world.children.calls if c.kind == "resume"][0]
    assert len(resume.item_ids) == 1 and "SEEDED-FEEDBACK-EVAL-1" in resume.prompt
    assert "First finding" not in resume.prompt and "Not needed." not in resume.prompt
    code, out = world.eval()  # no findings this time: fewer findings is a change, not a warning
    assert code == 0 and "changed: EVAL-1: Codex review findings 2 -> 0" in out
