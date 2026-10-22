"""A whole fake world for the runner tests (T9): children, GitHub, Linear, a
clock and a secret scanner, around a disposable source repo with a bare repo
standing in for GitHub's git side. No real model, GitHub or Linear call.

The fake test runner doesn't run pytest: it writes pytest-shaped JUnit (xunit2)
from simple rules about the checked-out files, so a run is instant and exact:
- tests/test_app.py::test_add always passes;
- tests/test_feature.py imports `feature` from app: without `def feature` in
  src/app/__init__.py the file fails at collection (ImportError naming it);
  with it, test_feature passes iff the source has `return 42`.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gaffer import children as ch
from gaffer import runner as rn
from gaffer import store as st
from gaffer import workspace as ws_mod
from gaffer.config import Secret
from gaffer.reviews.github import GitHubError

from workspace_helpers import TEST_ENV, make_config, make_remote, make_source, run_git, write


class Crash(BaseException):
    """Simulates gaffer dying mid-step: nothing in the runner catches it."""


# --- the scripted repo ----------------------------------------------------------

FEATURE_TEST = "from app import feature\n\n\ndef test_feature():\n    assert feature() == 42\n"
GOOD_SOURCE = "def add(a, b):\n    return a + b\n\n\ndef feature():\n    return 42\n"
BAD_SOURCE = "def add(a, b):\n    return a + b\n\n\ndef feature():\n    return 41\n"

TESTER_OUTPUT = {
    "criteria": [{"id": "C1", "text": "feature() returns 42"}],
    "untestable": [],
    "tests": [{"criterion_id": "C1", "label": "acceptance", "file": "tests/test_feature.py",
               "classname": "tests.test_feature", "name": "test_feature"}],
    "expected_missing": ["app.feature"],
    "files": [{"path": "tests/test_feature.py", "content": FEATURE_TEST}],
}

ISSUE_BODY = "Add feature().\n\n## Acceptance criteria\n\n- [ ] feature() returns 42\n"


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def pytest_junit(root: Path) -> str:
    cases = ['<testcase classname="tests.test_app" name="test_add" time="0.001" />']
    src = (root / "src/app/__init__.py").read_text() if (root / "src/app/__init__.py").exists() else ""
    if (root / "tests/test_feature.py").exists():
        if "def feature" not in src:
            text = ("ImportError while importing test module 'tests/test_feature.py'.\n"
                    "tests/test_feature.py:1: in <module>\n    from app import feature\n"
                    "E   ImportError: cannot import name 'feature' from 'app' (src/app/__init__.py)")
            cases.append(f'<testcase classname="" name="tests.test_feature" time="0.0">'
                         f'<error message="collection failure">{_xml(text)}</error></testcase>')
        elif "return 42" in src:
            cases.append('<testcase classname="tests.test_feature" name="test_feature" time="0.001" />')
        else:
            text = ("def test_feature():\n>       assert feature() == 42\nE       assert 41 == 42\n"
                    "E        +  where 41 = feature()\n\ntests/test_feature.py:5: AssertionError")
            cases.append(f'<testcase classname="tests.test_feature" name="test_feature" time="0.001">'
                         f'<failure message="assert 41 == 42">{_xml(text)}</failure></testcase>')
    for extra in sorted((root / "tests").glob("test_extra_*.py")) if (root / "tests").is_dir() else ():
        cases.append(f'<testcase classname="tests.{extra.stem}" name="test_it" time="0.0" />')
    if (root / "src/app/broken.py").exists():
        cases.append('<testcase classname="tests.test_app" name="test_regression" time="0.0">'
                     '<failure message="boom">E   AssertionError: boom\n\ntests/test_app.py:9: AssertionError</failure></testcase>')
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest">'
            + "".join(cases) + "</testsuite></testsuites>")


# --- clock and scanner -------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall_time = 1_760_000_000.0

    def monotonic(self) -> float:
        return self.mono

    def wall(self) -> float:
        return self.wall_time

    def sleep(self, seconds: float) -> None:
        self.mono += seconds
        self.wall_time += seconds


class FakeScanner:
    """workspace.SecretScanner: flags FAKESECRET_ tokens."""

    def __init__(self) -> None:
        self.scans = 0

    def scan(self, text: str) -> list[ws_mod.RawFinding]:
        self.scans += 1
        out = []
        for n, line in enumerate(text.split("\n"), start=1):
            if re.search(r"FAKESECRET_[A-Z0-9]{12}", line):
                out.append(ws_mod.RawFinding("fake-rule", "Fake secret", n))
        return out


# --- children ------------------------------------------------------------------------


def _lease(role: str, pid: int, session: str | None, cwd: Path) -> ch.ChildLease:
    return ch.ChildLease(role=role, pid=pid, pgid=pid, start_time=f"start-{pid}", cwd=str(cwd), session_id=session,
                         started_at=time.time())


def codex_run(role: str, lease: ch.ChildLease, data: Any, error: str | None = None, limit: ch.LimitEvent | None = None) -> ch.CodexRun:
    return ch.CodexRun(role=role, lease=lease, exit_code=0 if error is None else 1, timed_out=False, duration=1.0,
                       thread_id="t-1", data=data, result_error=error, limit=limit, usage={"input_tokens": 10},
                       rate_limits=None, model=None, log_path=None, stderr_tail="")


INIT = {"type": "system", "subtype": "init", "plugins": [], "mcp_servers": [], "apiKeySource": "none",
        "skills": ["code-loop", "address-pr-feedback"], "claude_code_version": "2.1.304"}


def writer_run(session: str, lease: ch.ChildLease, result: dict | None, *, item_ids=None, error: str | None = None,
               limit: ch.LimitEvent | None = None, init: dict | None = INIT, group_terminated: bool = False) -> ch.WriterRun:
    parsed = None
    if result is not None and error is None:
        try:
            parsed = ch.parse_writer_result(result, item_ids=item_ids)
        except ch.ResultError as e:
            error = f"malformed writer result: {e}"
    return ch.WriterRun(session_id=session, lease=lease, exit_code=0, timed_out=False, duration=5.0, init=init,
                        result_event=None, result=parsed, result_error=error, limit=limit, model="opus",
                        claude_version="2.1.304", skills=tuple((init or {}).get("skills") or ()),
                        usage={"output_tokens": 100}, num_turns=3, permission_denials=(), log_path=None,
                        group_terminated=group_terminated, stderr_tail="")


@dataclass
class WriterCall:
    kind: str  # "launch" | "resume"
    session_id: str
    prompt: str
    item_ids: list[str] | None
    plugin_keys: list[str]
    clone: Path


def commit(clone: Path, files: dict[str, str | None], message: str = "implement feature") -> str:
    for rel, content in files.items():
        if content is None:
            (clone / rel).unlink()
        else:
            write(clone, rel, content)
    run_git(clone, "add", "-A")
    run_git(clone, "commit", "-q", "-F", "-", input=message)
    return run_git(clone, "rev-parse", "HEAD")


def implement_good(call: WriterCall) -> dict:
    if (call.clone / "src/app/__init__.py").read_text() != GOOD_SOURCE:  # a continued session may have done it already
        commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE})
    return {"status": "done", "summary": "implemented feature", "question": None}


def implement_bad(call: WriterCall) -> dict:
    commit(call.clone, {"src/app/__init__.py": BAD_SOURCE})
    return {"status": "done", "summary": "implemented feature (wrongly)", "question": None}


def address_fix(call: WriterCall) -> dict:
    """Fix every item with one commit (making the feature correct too)."""
    sha = commit(call.clone, {"src/app/__init__.py": GOOD_SOURCE, f"src/app/fix_{len(call.item_ids or [])}_{time.time_ns()}.py": "x = 1\n"},
                 "address feedback")
    return {"status": "done", "summary": "fixed", "question": None,
            "dispositions": [{"item_id": i, "disposition": "fixed", "commit_sha": sha, "reason": "fixed"} for i in call.item_ids or []]}


def address_decline(call: WriterCall) -> dict:
    return {"status": "done", "summary": "declined", "question": None,
            "dispositions": [{"item_id": i, "disposition": "declined", "commit_sha": None, "reason": "not a real problem"}
                             for i in call.item_ids or []]}


class FakeChildren:
    """children.py's port. Scripted per role; records every call."""

    def __init__(self, world: World):
        self.world = world
        self.preflight_error: list[str] | None = None
        self.tester_outputs: list[Any] = []  # each: dict (raw output), "error", Crash, LimitEvent
        self.writer_actions: list[Callable[[WriterCall], Any]] = []  # launch/resume actions in order
        self.default_implement = implement_good
        self.default_address = address_fix
        self.reviews: list[Any] = []  # each: dict result or "error"
        self.calls: list[WriterCall] = []
        self.tester_calls = 0
        self.reviewer_calls = 0
        self.test_runs: list[Path] = []
        self.installs: list[Path] = []
        self.init_failures: list[dict] = []  # init events that kill the next writer launches
        self.next_pid = 900_000
        self.alive: dict[int, str] = {}  # pid -> start time: the leader is alive
        self.alive_groups: set[int] = set()  # pgids with members still running
        self.terminated: list[int] = []
        self.crash_after_test_runs: int | None = None

    def _pid(self) -> int:
        self.next_pid += 1
        return self.next_pid

    # preflight
    def preflight(self, repo):
        if self.preflight_error:
            raise ch.PreflightError(self.preflight_error)
        return ch.PreflightReport(claude_version="2.1.304 (Claude Code)", codex_version="codex-cli 0.160.0",
                                  gitleaks_version="8.30.1", srt_version=None,
                                  claude_auth={"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"},
                                  codex_login="Logged in using ChatGPT", mcp_servers=())

    # Codex
    def run_tester(self, jobs, prompt, *, on_spawn):
        self.tester_calls += 1
        lease = _lease("tester", self._pid(), None, jobs.clone)
        on_spawn(lease)
        out = self.tester_outputs.pop(0) if self.tester_outputs else TESTER_OUTPUT
        if isinstance(out, type) and issubclass(out, BaseException):
            raise out()
        if isinstance(out, ch.LimitEvent):
            return ch.TesterRun(codex_run("tester", lease, None, "limit", out), None, "limit")
        if out == "error":
            return ch.TesterRun(codex_run("tester", lease, None, "tester exited 1 without a result"), None,
                                "tester exited 1 without a result")
        try:
            return ch.TesterRun(codex_run("tester", lease, out), ch.parse_tester_result(out), None)
        except ch.ResultError as e:
            return ch.TesterRun(codex_run("tester", lease, out), None, f"invalid tester result: {e}")

    def run_reviewer(self, jobs, prompt, *, on_spawn):
        self.reviewer_calls += 1
        lease = _lease("reviewer", self._pid(), None, jobs.clone)
        on_spawn(lease)
        out = self.reviews.pop(0) if self.reviews else {"summary": "looks fine", "findings": [], "harness_judgements": []}
        if out == "error":
            return ch.ReviewRun(codex_run("reviewer", lease, None, "reviewer timed out"), None, "reviewer timed out")
        return ch.ReviewRun(codex_run("reviewer", lease, out), ch.parse_review_result(out), None)

    # writer
    def _writer(self, kind, repo, jobs, prompt, session_id, item_ids, on_spawn, plugin_keys):
        lease = _lease("writer", self._pid(), session_id, jobs.clone)
        call = WriterCall(kind, session_id, prompt, list(item_ids) if item_ids is not None else None, list(plugin_keys), jobs.clone)
        self.calls.append(call)
        on_spawn(lease)
        if self.init_failures:  # each entry kills one launch at its init event (R28)
            init = self.init_failures.pop(0)
            raise ch.InitCheckFailed(ch.init_problems(init, ()), init, lease)
        if self.writer_actions:
            action = self.writer_actions.pop(0)
        else:
            action = self.default_address if item_ids is not None else self.default_implement
        out = action(call)
        if isinstance(out, ch.WriterRun):
            return out
        return writer_run(session_id, lease, out, item_ids=item_ids)

    def launch_writer(self, repo, jobs, prompt, *, session_id, item_ids, on_spawn, plugin_keys):
        return self._writer("launch", repo, jobs, prompt, session_id, item_ids, on_spawn, plugin_keys)

    def resume_writer(self, repo, jobs, session_id, prompt, *, item_ids, on_spawn, plugin_keys):
        return self._writer("resume", repo, jobs, prompt, session_id, item_ids, on_spawn, plugin_keys)

    # gaffer's own runs
    def run_test_command(self, repo, jobs, cwd, *, command, on_spawn):
        lease = _lease("test", self._pid(), None, cwd)
        on_spawn(lease)
        assert command.endswith(repo.test_command), command
        self.test_runs.append(Path(cwd))
        if self.crash_after_test_runs is not None and len(self.test_runs) > self.crash_after_test_runs:
            self.crash_after_test_runs = None  # crash once
            raise Crash()
        (Path(cwd) / repo.junit_path).write_text(pytest_junit(Path(cwd)))
        return ch.CommandRun(command, Path(cwd), lease, 1, False, 0.5, "", "", None)

    def run_install_command(self, repo, jobs, checkout, *, hosts, on_spawn):
        lease = _lease("install", self._pid(), None, checkout)
        on_spawn(lease)
        self.installs.append(Path(checkout))
        return ch.CommandRun(repo.install_command, Path(checkout), lease, 0, False, 0.1, "installed", "", None)

    # liveness
    def start_time(self, pid):
        if pid < 900_000:
            return ch.process_start_time(pid)
        return self.alive.get(pid)

    def group_alive(self, pgid):
        return pgid in self.alive or pgid in self.alive_groups

    def wait_group(self, pgid, timeout):
        return False

    def terminate_group(self, pgid):
        self.terminated.append(pgid)
        self.alive.pop(pgid, None)
        self.alive_groups.discard(pgid)
        return True


# --- GitHub ---------------------------------------------------------------------------------


class FakeGitHub:
    """workspace.GitHubClient's transport and reviews.GitHubReader over one shared state.
    The PR's head follows the bare remote, so a commit pushed there by "Mark" is a foreign head."""

    def __init__(self, world: World):
        self.world = world
        self.github = "mfullbrook/app"
        self.login = "mfullbrook"
        self.pulls: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.rc_list: list[dict[str, Any]] = []
        self.threads_list: list[dict[str, Any]] = []
        self.reviews_list: list[dict[str, Any]] = []
        self.statuses: Callable[[str], list[dict]] = self.default_statuses
        self.check_runs_for: Callable[[str], list[dict]] = self.default_check_runs
        self.logs: dict[int, bytes] = {}
        self.requests: list[str] = []
        self.polls = 0
        self.on_poll: Callable[[FakeGitHub, int], None] | None = None
        self.fail_reads = 0
        self._next = 5000

    def next_id(self) -> int:
        self._next += 1
        return self._next

    # -- defaults --
    def default_statuses(self, sha: str) -> list[dict]:
        return [{"id": 1, "context": "CodeRabbit", "state": "success", "description": "Review completed",
                 "creator": {"login": "coderabbitai[bot]"}, "created_at": "2026-10-22T00:00:00Z"}]

    def default_check_runs(self, sha: str) -> list[dict]:
        return [{"id": 77, "name": "test", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"},
                 "output": {"title": "ok", "summary": "ok", "annotations_count": 0}, "started_at": "2026-10-22T00:00:00Z"}]

    # -- helpers --
    def remote_sha(self, branch: str) -> str | None:
        out = subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=self.world.remote,
                             env=TEST_ENV, capture_output=True, text=True)
        return out.stdout.strip() or None

    def pr(self) -> dict[str, Any]:
        assert len(self.pulls) >= 1
        return list(self.pulls.values())[-1]

    def add_inline(self, body: str, *, login: str = "coderabbitai[bot]", path: str = "src/app/__init__.py", line: int = 5,
                   resolved: bool = False) -> int:
        cid = self.next_id()
        self.rc_list.append({"id": cid, "user": {"login": login}, "body": body, "path": path, "line": line,
                                     "in_reply_to_id": None, "html_url": f"https://github.com/x#r{cid}"})
        self.threads_list.append({"id": f"T{cid}", "isResolved": resolved, "isOutdated": False, "path": path, "line": line,
                                    "comments": {"nodes": [{"databaseId": cid, "author": {"login": login.removesuffix('[bot]')}}]}})
        return cid

    def add_issue_comment(self, number: int, body: str, login: str = "mfullbrook") -> dict:
        c = {"id": self.next_id(), "body": body, "user": {"login": login}, "html_url": f"https://github.com/x#c{self._next}"}
        self.comments.setdefault(number, []).append(c)
        return c

    def round_comments(self) -> list[dict]:
        return [c for cs in self.comments.values() for c in cs if "gaffer-round: " in c["body"]]

    # -- transport for workspace.GitHubClient --
    def __call__(self, method, url, headers, body, timeout):
        self.requests.append(f"{method} {url.split('?')[0]}")
        payload = json.loads(body) if body else None
        base = f"https://api.github.com/repos/{self.github}"
        path, _, query = url.partition("?")
        params = dict(urllib.parse.parse_qsl(query))

        def resp(status, data):
            return ws_mod.HttpResponse(status, {}, json.dumps(data).encode())

        if path == f"{base}/pulls" and method == "GET":
            owner = self.github.split("/")[0]
            found = [self._view(p) for p in self.pulls.values()
                     if f"{owner}:{p['head']['ref']}" == params.get("head") and p["state"] == params.get("state", "open")]
            return resp(200, found)
        if path == f"{base}/pulls" and method == "POST":
            number = 100 + len(self.pulls)
            self.pulls[number] = {"number": number, "html_url": f"https://github.com/{self.github}/pull/{number}",
                                  "state": "open", "merged": False, "draft": payload.get("draft", False),
                                  "body": payload["body"], "title": payload["title"], "head": {"ref": payload["head"]},
                                  "base": {"ref": payload["base"]}, "mergeable": True}
            return resp(201, self._view(self.pulls[number]))
        m = re.fullmatch(rf"{re.escape(base)}/issues/(\d+)/comments", path)
        if m and method == "GET":
            return resp(200, self.comments.get(int(m.group(1)), []))
        if m and method == "POST":
            return resp(201, self.add_issue_comment(int(m.group(1)), payload["body"], self.login))
        m = re.fullmatch(rf"{re.escape(base)}/pulls/(\d+)", path)
        if m and method == "GET":
            return resp(200, self._view(self.pulls[int(m.group(1))]))
        return resp(404, {"message": "Not Found"})

    def _view(self, p: dict) -> dict:
        v = dict(p)
        v["head"] = {**p["head"], "sha": self.remote_sha(p["head"]["ref"])}
        return v

    # -- reviews.GitHubReader --
    def _read(self, what: str):
        self.requests.append(f"READ {what}")
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise GitHubError(f"{what}: HTTP 502")

    def pull(self, repo, number):
        self._read("pull")
        self.polls += 1
        if self.on_poll is not None:
            self.on_poll(self, self.polls)
        return self._view(self.pulls[number])

    def commit_statuses(self, repo, sha):
        self._read("statuses")
        return self.statuses(sha)

    def check_runs(self, repo, sha):
        self._read("check_runs")
        return self.check_runs_for(sha)

    def check_run_annotations(self, repo, check_run_id):
        self._read("annotations")
        return []

    def reviews(self, repo, number):
        self._read("reviews")
        return list(self.reviews_list)

    def review_comments(self, repo, number):
        self._read("review_comments")
        return list(self.rc_list)

    def issue_comments(self, repo, number):
        self._read("issue_comments")
        return list(self.comments.get(number, []))

    def review_threads(self, repo, number):
        self._read("review_threads")
        return list(self.threads_list)

    def job_log(self, repo, job_id):
        self._read("job_log")
        if job_id not in self.logs:
            raise GitHubError("no log")
        return self.logs[job_id]


class FakeLinear:
    def __init__(self) -> None:
        self.issues: dict[str, rn.Issue] = {}
        self.calls: list[str] = []

    def fetch_issue(self, key: str) -> rn.Issue:
        self.calls.append(key)
        return self.issues[key]


# --- the world ----------------------------------------------------------------------------


class World:
    def __init__(self, tmp: Path, *, ecosystems: str = '["python"]', runner: str = "pytest", extra: str = ""):
        self.tmp = tmp
        self.src = make_source(tmp)
        self.remote = make_remote(tmp, self.src)
        self.config = make_config(tmp, self.src, ecosystems=ecosystems, runner=runner, extra=extra)
        self.home = self.config.home
        self.repo = self.config.repos["ENG"]
        self.clock = FakeClock()
        self.scanner = FakeScanner()
        self.children = FakeChildren(self)
        self.gh = FakeGitHub(self)
        self.linear = FakeLinear()
        self.linear.issues["ENG-1"] = rn.Issue("ENG-1", "Add feature", ISSUE_BODY)
        self.linear.issues["ENG-2"] = rn.Issue("ENG-2", "Another", ISSUE_BODY)
        self.outputs: list[str] = []
        self.sleeps: list[float] = []

    @property
    def db(self) -> Path:
        return st.default_db_path(self.home)

    def store(self) -> st.Store:
        return st.Store(self.db, clock=self.clock)

    def deps(self, store: st.Store, out: io.StringIO) -> rn.Deps:
        def github(repo):
            return rn.GitHubPorts(ws_mod.GitHubClient(self.gh.github, Secret("ghp_fake"), transport=self.gh), self.gh)

        def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            if len(self.sleeps) > 500:
                raise AssertionError("runaway poll loop")
            self.clock.sleep(seconds)

        return rn.Deps(cfg=self.config, store=store, children=self.children, github=github, linear=lambda: self.linear,
                       scanner=self.scanner, clock=self.clock, sleep=sleep, home=self.home, token=lambda: None,
                       remote_url=lambda repo: str(self.remote), rng=__import__("random").Random(0), out=out)

    def run(self, key: str = "ENG-1", **opts: Any) -> tuple[int | str, str]:
        """One `gaffer run` as its own process would: a fresh store connection and runner."""
        out = io.StringIO()
        store = self.store()
        try:
            code: int | str = rn.Runner(self.deps(store, out)).run(key, rn.Options(**opts))
        except Crash:
            code = "crash"
        finally:
            store.close()
        text = out.getvalue()
        self.outputs.append(text)
        return code, text

    def job(self, key: str = "ENG-1") -> st.Job:
        with self.store() as s:
            return s.job_for_issue(key)

    def events(self, kind: str, key: str = "ENG-1") -> list[st.Event]:
        with self.store() as s:
            j = s.job_for_issue(key)
            return s.events(j.id, kind)

    def clone(self, key: str = "ENG-1") -> Path:
        return Path(self.job(key).clone_path)

    def remote_head(self, branch: str = "gaffer/ENG-1") -> str | None:
        return self.gh.remote_sha(branch)

    def mark_pushes(self, files: dict[str, str], branch: str = "gaffer/ENG-1", message: str = "mark's fix") -> str:
        """Mark commits on the PR branch himself and pushes it (a foreign head)."""
        work = self.tmp / f"mark-{time.time_ns()}"
        subprocess.run(["git", "clone", "-q", "-b", branch, str(self.remote), str(work)], env=TEST_ENV, check=True,
                       capture_output=True)
        sha = commit(work, files, message)
        run_git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return sha

    def report(self, key: str = "ENG-1") -> str:
        j = self.job(key)
        return (self.home / ".gaffer" / "jobs" / rn.job_marker(j) / "report.md").read_text()
