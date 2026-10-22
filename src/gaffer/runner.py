"""`gaffer run <ISSUE>`: the job state machine, reconciliation, locks, exit
codes, rerun flags, the round summary comment, the report and the read-only
Linear fetch.

Decisions: R8 (exit codes, rerun flags, cycles, round timer, not_ready,
READY/CLOSED reruns), R9 (flocks, liveness for every child role, the writer
process-group wait before ROUND, pre-assigned session IDs, round marker,
state_lost), R10 (writer gates and the one nudge), R24 (local repair loop
counts as a cycle; a head that fails VERIFY is never published), R25
(supervised-time round deadlines), R27 (CodeRabbit verified/unverified), R28
(synced skills in the report), R29 (GitHub hosts open for PHP repos), R30 (CI
and harness changes listed prominently), R31 (crashed active job refusal,
plugin block list and one relaunch, `.git/config` restores in the report,
Codex findings as packet items, the `<job>` marker), D1 (Linear read-only),
D2 (one summary comment per round), Arch #3 (argv-only issue key).

The job flow ("v0.1 plan as amended by eng review"):

    PREFLIGHT -> INTAKE -> WORKSPACE -> TESTER -> RED_FIRST -> WRITER -> (gates)
      -> VERIFY --fail--> local packet -> RESUME -> (gates) -> VERIFY ...
      -> PUBLISH -> ROUND -> PACKET -> RESUME -> (gates) -> VERIFY -> PUBLISH -> ...
      -> READY (0) | BLOCKED (2) | CLOSED (3); internal error 1; refused 4

Every step is re-entrant: it checks real state (store, job clone, remote,
GitHub) before any side effect, so a rerun after a crash continues without a
second PR, writer or comment (design "Liveness and recovery", SC4).

The runner talks to the other modules through small ports so tests can run
the whole flow with fakes: `ChildrenPort` (children.py), `GitHubPorts`
(workspace.GitHubClient for writes, reviews.GitHubReader for rounds),
`LinearPort`, a gitleaks scanner, an injectable clock, sleep and jitter.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, TextIO

from gaffer import children as ch
from gaffer import config as gconfig
from gaffer import store as st
from gaffer import workspace as ws_mod
from gaffer.config import Config, RepoConfig, Secret
from gaffer.reviews import checks as rv_checks
from gaffer.reviews import coderabbit as rv_cr
from gaffer.reviews import github as rv_github
from gaffer.reviews import junit as rv_junit
from gaffer.reviews import packet as rv_packet
from gaffer.reviews import redfirst as rv_redfirst
from gaffer.reviews import rounds as rv_rounds

# --- exit codes (R8) -----------------------------------------------------------

EXIT_READY = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2
EXIT_CLOSED = 3
EXIT_REFUSED = gconfig.EXIT_REFUSED  # 4

# Arch #3: the issue key arrives on argv only and must match exactly (fullmatch, so "ENG-1\n" fails).
ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9]*-\d+")

POLL_SECONDS = 60.0  # design "Waits and timeouts": poll every 60 s with jitter
POLL_JITTER = 10.0
WRITER_GROUP_GRACE = 120.0  # R9: before ROUND, wait up to 2 min for the writer's group, then SIGTERM

LINEAR_API = "https://api.linear.app/graphql"
LINEAR_TOKEN_ACCOUNT = "linear-token"  # Keychain: service "gaffer", account "linear-token"
PLUGIN_BLOCKLIST = "blocked-plugins.json"  # R31: under ~/.gaffer, persistent across jobs

# BLOCKED reasons this runner can produce that store.BLOCKED_REASONS (T6) doesn't
# list yet: workspace's own codes (T7 report item 2) plus isolation_failed (R31)
# and install_failed. ADAPTER: store.block() validates against the module-level
# frozenset at call time, so the runner widens it here instead of editing store.py.
RUNNER_BLOCKED_REASONS = frozenset({
    "isolation_failed",  # R31: init check failed again after the one relaunch
    "install_failed",  # install_command failed in the job clone or a verification worktree
    ws_mod.WRITER_BRANCH_MOVED,
    ws_mod.WRITER_HISTORY_REWRITTEN,
    ws_mod.WORKSPACE_TAMPERED,
    ws_mod.SECRET_SCAN_FAILED,
    ws_mod.FOREIGN_PR,
})
st.BLOCKED_REASONS = st.BLOCKED_REASONS | RUNNER_BLOCKED_REASONS

# What the report says for each BLOCKED reason, and what fixes it. The step in
# brackets is where a rerun re-validates from.
BLOCKED_HELP: dict[str, str] = {
    "acceptance_criteria_missing": "The Linear issue has no \"Acceptance criteria\" heading or checklist. Add one to the issue and rerun.",
    "tester_failed": "The Codex tester errored, timed out or returned nothing twice. Check the tester log and rerun.",
    "tester_invalid": "The tester's tests were invalid twice (bad paths, wrong red-first reason, missing coverage). Rerun to try a fresh tester.",
    "already_passing": "Acceptance tests already pass on the base: the issue may already be fixed. Check the listed tests.",
    "no_testable_criteria": "The tester judged every criterion untestable. Make the criteria testable and rerun.",
    "writer_no_result": "The writer returned no valid structured result (or cited a commit that isn't new). Rerun to resume it.",
    "writer_no_change": "The writer made no commit (or marked items fixed without one). Rerun to resume it.",
    "writer_dirty": "The writer left uncommitted changes after one nudge. Rerun to resume it.",
    "writer_blocked": "The writer stopped with a question (printed below). Answer it in the issue and rerun.",
    "tester_commit_rewritten": "The tester commit is no longer an ancestor of the head. Restore it and rerun.",
    "tester_change_unexplained": "A writer commit changed a tester, harness or CI file without a Tester-Change-Reason: trailer. Rerun to resume the writer.",
    "forbidden_path": "A writer commit touched .claude/, .codex/ or .mcp.json. Remove the commit and rerun.",
    "secret_detected": "gitleaks found a secret (redacted below) in a push, comment or packet. Remove it and rerun.",
    "verify_failed": "gaffer's own test run kept failing and the repair cycles ran out; nothing was published. Rerun with --more-cycles N.",
    "repair_limit": "Review feedback remains and the repair cycles ran out. Rerun with --more-cycles N.",
    "not_ready": "The round completed with nothing to resume but READY's conditions (listed below) aren't met. Fix them and rerun.",
    "review_timeout": "The round didn't complete within round_timeout supervised seconds. Rerun to open a new round on the current head.",
    "reviewer_failed": "The Codex reviewer failed twice for this head. Rerun to retry it.",
    "required_check_missing": "A required check never appeared on the head within check_appear_timeout. Fix the workflow or gaffer.toml and rerun.",
    "foreign_head": "The PR head is a commit gaffer didn't push. Rerun with --adopt-head to accept it.",
    "base_conflict": "The PR conflicts with the base. Merge or rebase it yourself, push, and rerun with --adopt-head.",
    "usage_limit": "A child hit a subscription usage limit. Rerun after the reset time.",
    "state_lost": "gaffer has no record of this issue's branch or PR (state.db lost). Rebuilding with --adopt-head isn't built yet (R9/R23, build step 5).",
    "stopped": "The job was stopped.",
    "unmapped_team": "The issue's team has no [repos.<TEAM>] table in gaffer.toml.",
    "isolation_failed": "A child's init event still showed a plugin, an unlisted MCP server or a non-subscription key after one relaunch (R28, R31). Check ~/.gaffer and rerun.",
    "install_failed": "install_command failed. Check the install log and rerun.",
    ws_mod.WRITER_BRANCH_MOVED: "The writer left HEAD detached or on another branch. Check out the job branch in the job clone and rerun.",
    ws_mod.WRITER_HISTORY_REWRITTEN: "The writer rewrote history gaffer already saw; gaffer never force-pushes. Restore the branch and rerun.",
    ws_mod.WORKSPACE_TAMPERED: "The job clone's .git was replaced. Remove the job dir and rerun with --fresh after closing the PR.",
    ws_mod.SECRET_SCAN_FAILED: "gitleaks failed to run, so nothing was published (fail closed). Fix gitleaks and rerun.",
    ws_mod.FOREIGN_PR: "An open PR for the branch doesn't carry this job's gaffer-job marker. Close it and rerun.",
}

# Where a rerun re-validates from, when it isn't simply the step that blocked.
RERUN_STEP: dict[str, str] = {
    "not_ready": "ROUND",  # a fresh round re-reads checks and items
    "review_timeout": "ROUND",  # R8: a rerun after review_timeout opens a new round
    "required_check_missing": "ROUND",
    "verify_failed": "VERIFY",
    "repair_limit": "PACKET",
    "acceptance_criteria_missing": "INTAKE",
}

# Store item sources (T6) vs reviews sources (T8). ADAPTER: the two modules
# name CI and test items differently.
_RV_TO_STORE_SOURCE = {"coderabbit": "coderabbit", "operator": "operator", "codex": "codex", "ci": "check", "tests": "test"}
# children.py lease roles vs store.CHILD_ROLES.
_STORE_ROLE = {"tester": "tester", "writer": "writer", "reviewer": "reviewer", "test": "test_run", "install": "install"}

Key = tuple[str, str, str]


# --- errors -----------------------------------------------------------------------


class Refused(Exception):
    """Exit 4: a lock is held, preflight failed, a child is alive, or a flag doesn't apply."""

    exit_code = EXIT_REFUSED


class Blocked(Exception):
    def __init__(self, reason: str, message: str, *, step: str | None = None, detail: Any = None):
        self.reason = reason
        self.message = message
        self.step = step
        self.detail = detail
        super().__init__(f"BLOCKED: {reason}: {message}")


class Closed(Exception):
    pass


class LinearError(Exception):
    exit_code = EXIT_ERROR


# --- options ----------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    """R8 rerun flags."""

    adopt_head: bool = False
    more_cycles: int = 0
    cont: bool = False  # --continue
    fresh: bool = False


# --- Linear (read-only, D1) ---------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    key: str
    title: str
    description: str
    url: str = ""

    @property
    def acceptance_criteria(self) -> str | None:
        return acceptance_criteria(self.description)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "description": self.description, "url": self.url}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Issue:
        return cls(d["key"], d.get("title") or "", d.get("description") or "", d.get("url") or "")


_AC_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6}[ \t]*|\*\*|__)?[ \t]*acceptance criteria\b.*$", re.IGNORECASE | re.MULTILINE)
_ANY_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+\S", re.MULTILINE)
_CHECKLIST = re.compile(r"^[ \t]*[-*+][ \t]+\[[ xX]\][ \t]+\S.*$", re.MULTILINE)


def acceptance_criteria(description: str | None) -> str | None:
    """Design INTAKE: the description has an "Acceptance criteria" heading (with
    something under it) or a checklist. Returns that text, or None."""
    text = (description or "").replace("\r\n", "\n")
    m = _AC_HEADING.search(text)
    if m:
        rest = text[m.end():]
        nxt = _ANY_HEADING.search(rest)
        body = (rest[: nxt.start()] if nxt else rest).strip()
        if body:
            return body
    items = _CHECKLIST.findall(text)
    if items:
        return "\n".join(i.strip() for i in items)
    return None


class LinearPort(Protocol):
    def fetch_issue(self, key: str) -> Issue: ...


_ISSUE_QUERY = "query($id: String!) { issue(id: $id) { identifier title description url } }"


class LinearClient:
    """A read-only Linear GraphQL client over urllib (D1: no write-back in v0.1).

    It sends exactly one query shape, `issue(id:)`, and never a mutation."""

    def __init__(self, token: Secret, *, opener: Callable[[urllib.request.Request, float], Any] | None = None,
                 api: str = LINEAR_API, timeout: float = 30.0):
        if not isinstance(token, Secret):
            raise TypeError("token must be a config.Secret")
        self._token = token
        self._open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t))
        self._api = api
        self._timeout = timeout

    def __repr__(self) -> str:
        return "LinearClient(<redacted>)"

    def fetch_issue(self, key: str) -> Issue:
        if not ISSUE_KEY.fullmatch(key):
            raise ValueError(f"{key!r} is not an issue key")
        body = json.dumps({"query": _ISSUE_QUERY, "variables": {"id": key}}).encode()
        req = urllib.request.Request(self._api, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "gaffer")
        # Linear personal API keys go in Authorization as-is (no "Bearer").
        req.add_unredirected_header("Authorization", self._token.reveal())
        try:
            with self._open(req, self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise LinearError(f"Linear returned HTTP {e.code} for {key}.") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LinearError(f"Linear request for {key} failed: {_scrub(str(e), self._token)}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise LinearError(f"Linear's answer for {key} is not JSON.") from None
        if data.get("errors"):
            msgs = "; ".join(str(e.get("message", e)) for e in data["errors"] if isinstance(e, dict))
            raise LinearError(f"Linear refused the query for {key}: {_scrub(msgs, self._token)}")
        issue = ((data.get("data") or {}).get("issue")) or None
        if not issue:
            raise LinearError(f"Linear has no issue {key}.")
        if issue.get("identifier") != key:
            raise LinearError(f"Linear answered with {issue.get('identifier')!r} for {key}.")
        return Issue(key, str(issue.get("title") or ""), str(issue.get("description") or ""), str(issue.get("url") or ""))


def _scrub(text: str, token: Secret) -> str:
    return text.replace(token.reveal(), "<redacted>")


def linear_token(keychain: gconfig.Keychain | None = None) -> Secret:
    """gaffer's own Linear API key from the login Keychain (service "gaffer",
    account "linear-token"). config.py has only github_token(); this mirrors it.
    Never logged and never in a child's environment."""
    keychain = keychain or gconfig.MacKeychain()
    value = keychain.find_generic_password(gconfig.KEYCHAIN_SERVICE, LINEAR_TOKEN_ACCOUNT)
    add = f"security add-generic-password -U -s {gconfig.KEYCHAIN_SERVICE} -a {LINEAR_TOKEN_ACCOUNT} -w"
    if value is None:
        raise gconfig.KeychainError(
            f"No Linear API key in the Keychain (service {gconfig.KEYCHAIN_SERVICE!r}, account {LINEAR_TOKEN_ACCOUNT!r}). "
            f"Fix: run `{add}` and paste a read-only Linear key at the prompt."
        )
    if not value or any(c.isspace() for c in value):
        raise gconfig.KeychainError(
            f"The Keychain item (service {gconfig.KEYCHAIN_SERVICE!r}, account {LINEAR_TOKEN_ACCOUNT!r}) is empty or contains whitespace. "
            f"Fix: re-run `{add}` and paste only the key."
        )
    return Secret(value)


# --- ports ----------------------------------------------------------------------------

OnSpawn = Callable[[ch.ChildLease], None]


class ChildrenPort(Protocol):
    """The slice of children.py the runner uses. RealChildren binds it to the config."""

    def preflight(self, repo: RepoConfig) -> ch.PreflightReport: ...
    def run_tester(self, jobs: ch.JobDirs, prompt: str, *, on_spawn: OnSpawn) -> ch.TesterRun: ...
    def run_reviewer(self, jobs: ch.JobDirs, prompt: str, *, on_spawn: OnSpawn) -> ch.ReviewRun: ...
    def launch_writer(self, repo: RepoConfig, jobs: ch.JobDirs, prompt: str, *, session_id: str,
                      item_ids: Sequence[str] | None, on_spawn: OnSpawn, plugin_keys: Sequence[str]) -> ch.WriterRun: ...
    def resume_writer(self, repo: RepoConfig, jobs: ch.JobDirs, session_id: str, prompt: str, *,
                      item_ids: Sequence[str] | None, on_spawn: OnSpawn, plugin_keys: Sequence[str]) -> ch.WriterRun: ...
    def run_test_command(self, repo: RepoConfig, jobs: ch.JobDirs, cwd: Path, *, command: str, on_spawn: OnSpawn) -> ch.CommandRun: ...
    def run_install_command(self, repo: RepoConfig, jobs: ch.JobDirs, checkout: Path, *, hosts: Sequence[str],
                            on_spawn: OnSpawn) -> ch.CommandRun: ...
    def start_time(self, pid: int) -> str | None: ...
    def group_alive(self, pgid: int) -> bool: ...
    def wait_group(self, pgid: int, timeout: float) -> bool: ...
    def terminate_group(self, pgid: int) -> bool: ...


class RealChildren:
    def __init__(self, cfg: Config, paths: ch.Paths | None = None):
        self.cfg = cfg
        self.paths = paths or ch.Paths.for_home(cfg.home)

    def preflight(self, repo):
        return ch.preflight(self.cfg, repo, paths=self.paths)

    def run_tester(self, jobs, prompt, *, on_spawn):
        return ch.run_tester(self.cfg, jobs, prompt, paths=self.paths, on_spawn=on_spawn)

    def run_reviewer(self, jobs, prompt, *, on_spawn):
        return ch.run_reviewer(self.cfg, jobs, prompt, paths=self.paths, on_spawn=on_spawn)

    def launch_writer(self, repo, jobs, prompt, *, session_id, item_ids, on_spawn, plugin_keys):
        return ch.launch_writer(self.cfg, repo, jobs, prompt, session_id=session_id, item_ids=item_ids, paths=self.paths,
                                on_spawn=on_spawn, plugin_keys=plugin_keys, group_grace=WRITER_GROUP_GRACE)

    def resume_writer(self, repo, jobs, session_id, prompt, *, item_ids, on_spawn, plugin_keys):
        return ch.resume_writer(self.cfg, repo, jobs, session_id, prompt, item_ids=item_ids, paths=self.paths,
                                on_spawn=on_spawn, plugin_keys=plugin_keys, group_grace=WRITER_GROUP_GRACE)

    def run_test_command(self, repo, jobs, cwd, *, command, on_spawn):
        return ch.run_test_command(self.cfg, repo, jobs, cwd, command=command, paths=self.paths, on_spawn=on_spawn)

    def run_install_command(self, repo, jobs, checkout, *, hosts, on_spawn):
        return ch.run_install_command(self.cfg, repo, jobs, checkout, hosts=hosts, paths=self.paths, on_spawn=on_spawn)

    def start_time(self, pid):
        return ch.process_start_time(pid)

    def group_alive(self, pgid):
        return ch.group_alive(pgid)

    def wait_group(self, pgid, timeout):
        return ch.wait_group(pgid, timeout)

    def terminate_group(self, pgid):
        return ch.terminate_group(pgid)


@dataclass
class GitHubPorts:
    client: ws_mod.GitHubClient  # PR, round summary comment (writes)
    reader: rv_github.GitHubReader  # round polling (reads)


class _ProbeAdapter:
    """store.ProcessProbe over the children port."""

    def __init__(self, children: ChildrenPort):
        self._children = children

    def start_time(self, pid: int) -> str | None:
        return self._children.start_time(pid)


class PacketScanner:
    """ADAPTER: reviews.packet.SecretScanner over workspace's gitleaks scanner.
    T7's Gitleaks.scan returns RawFinding(rule_id, description, line); T8's
    packet wants SecretFinding(rule, description)."""

    def __init__(self, scanner: ws_mod.SecretScanner):
        self._scanner = scanner

    def scan(self, text: str) -> list[rv_packet.SecretFinding]:
        return [rv_packet.SecretFinding(f.rule_id, f.description) for f in self._scanner.scan(text)]


class Clock(Protocol):
    def wall(self) -> float: ...
    def monotonic(self) -> float: ...


@dataclass
class Deps:
    cfg: Config
    store: st.Store
    children: ChildrenPort
    github: Callable[[RepoConfig], GitHubPorts]
    linear: Callable[[], LinearPort]
    scanner: ws_mod.SecretScanner
    clock: Clock
    sleep: Callable[[float], None]
    home: Path
    token: Callable[[], Secret | None] = lambda: None  # gaffer's GitHub token for git push / ls-remote
    remote_url: Callable[[RepoConfig], str] | None = None  # tests point origin at a bare repo
    rng: random.Random = field(default_factory=random.Random)
    out: TextIO = field(default_factory=lambda: sys.stdout)


# --- locks (R9) ---------------------------------------------------------------------


def gaffer_dir(home: Path) -> Path:
    return home / ".gaffer"


def job_lock_path(home: Path, key: str) -> Path:
    return gaffer_dir(home) / "jobs" / f"{key}.lock"


def run_lock_path(home: Path) -> Path:
    return gaffer_dir(home) / "run.lock"


def _holder(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 200).decode(errors="replace").strip()
    except OSError:
        return ""


def _flock(path: Path, label: str, what: str) -> int:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = _holder(fd)
        os.close(fd)
        pid, _, held_for = holder.partition(" ")
        who = f"pid {pid}" if pid.isdigit() else "another gaffer process (pid not yet written)"
        job = f" for {held_for}" if held_for else ""
        raise Refused(f"{label} {path} is held by {who}{job}; {what}. "
                      f"Wait for it to finish, or stop that process.") from None
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def run_locks(home: Path, key: str) -> Iterator[None]:
    """R9: flock ~/.gaffer/jobs/<key>.lock for the life of `gaffer run`, plus
    ~/.gaffer/run.lock for one job at a time. A held lock is exit 4 naming the
    holder PID. flock releases on any exit, crash included."""
    fds: list[int] = []
    try:
        fd = _flock(job_lock_path(home, key), "the job lock", f"another `gaffer run {key}` is already driving this job")
        _stamp_lock(fd, key)
        fds.append(fd)
        fd = _flock(run_lock_path(home), "the run lock", "gaffer runs one job at a time")
        _stamp_lock(fd, key)
        fds.append(fd)
        yield
    finally:
        for fd in reversed(fds):
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def _stamp_lock(fd: int, key: str) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()} {key}\n".encode())


# --- helpers ---------------------------------------------------------------------------


def job_marker(job: st.Job) -> str:
    """R31: `<job>` in the gaffer-job and gaffer-round markers, the job dir and
    the state dir: the issue key plus the --fresh generation (`ENG-12`, `ENG-12.2`),
    never the DB row id. ADAPTER: store.Job.ref writes `ENG-12-2`; the runner
    uses R31's dotted form everywhere it writes a marker."""
    return job.issue_key if job.generation == 1 else f"{job.issue_key}.{job.generation}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _short(sha: str | None) -> str:
    return (sha or "")[:12]


def _now_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


class _Writer:
    """What one writer step produced: the parsed result and the gate result."""

    def __init__(self, result: ch.WriterResult, gate: ws_mod.GateResult, session_id: str):
        self.result = result
        self.gate = gate
        self.session_id = session_id


# --- runner -------------------------------------------------------------------------


class Runner:
    def __init__(self, deps: Deps):
        self.d = deps
        self.cfg = deps.cfg
        self.store = deps.store
        self.children = deps.children
        self._ws: dict[int, ws_mod.Workspace] = {}
        self._gh: dict[str, GitHubPorts] = {}
        self._token: Secret | None | bool = False
        self._preflight: ch.PreflightReport | None = None

    # -- entry --------------------------------------------------------------------

    def run(self, key: str, opts: Options = Options()) -> int:
        if not ISSUE_KEY.fullmatch(key):
            self._say(f"gaffer: {key!r} is not a Linear issue key like ENG-123 (refused).")
            return EXIT_REFUSED
        try:
            with run_locks(self.d.home, key):
                return self._run_locked(key, opts)
        except Refused as e:
            self._say(f"gaffer: refused: {e}")
            return EXIT_REFUSED
        except (gconfig.ConfigError, gconfig.KeychainError, ch.ChildError, st.StoreError) as e:
            code = getattr(e, "exit_code", EXIT_ERROR)
            self._say(f"gaffer: {'refused' if code == EXIT_REFUSED else 'error'}: {e}")
            return code

    def _run_locked(self, key: str, opts: Options) -> int:
        store = self.store
        team = key.rsplit("-", 1)[0]
        repo = self.cfg.repos.get(team)
        if repo is None:
            raise Refused(f"{key}: team {team} has no [repos.{team}] table in {self.cfg.path}. "
                          f"Fix: add one, then rerun `gaffer run {key}`.")
        if opts.more_cycles < 0:
            raise Refused("--more-cycles needs a positive N.")

        # R31: a crashed active job (we hold run.lock, so nothing is driving it) refuses other issues.
        active = store.active_job()
        if active is not None and active.issue_key != key:
            raise Refused(
                f"job {job_marker(active)} is still active (state {active.state}) after an interrupted run; "
                f"gaffer runs one job at a time. Fix: finish it with `gaffer run {active.issue_key}` "
                f"(or, once it exists, `gaffer stop {active.issue_key}`), then rerun `gaffer run {key}`.")

        job = store.job_for_issue(key)
        self._check_flags(job, opts)

        if job is not None and job.state == "READY" and not opts.cont:
            # R8: a READY rerun reprints the stored report without touching GitHub.
            self._print_report(job)
            return EXIT_READY
        if job is not None and job.state == "CLOSED" and not opts.fresh:
            self._print_report(job)
            self._say(f"gaffer: {key} is CLOSED (its PR was closed or merged). Rerun with --fresh to start "
                      f"a new job on branch {st.branch_name(key, job.generation + 1)}.")
            return EXIT_CLOSED

        self._reap_leases(job)

        try:
            report = self.children.preflight(repo)
        except ch.PreflightError as e:
            raise Refused(str(e)) from None
        self._preflight = report

        if job is not None and job.state == "CLOSED" and opts.fresh:
            store.archive_job(job.id)
            job = None
        if job is None:
            job = self._new_job(key, repo)
            if job.state == "BLOCKED":  # state_lost
                return self._finish(job)

        store.record_cli_evidence(job.id, claude_version=report.claude_version, codex_version=report.codex_version,
                                  claude_auth=ch.redact(dict(report.claude_auth)), codex_auth=ch.redact(report.codex_login),
                                  passed=True, extra={"gitleaks": report.gitleaks_version, "srt": report.srt_version,
                                                      "mcp_servers": list(report.mcp_servers)})
        if opts.more_cycles:
            job = store.grant_cycles(job.id, opts.more_cycles, note="--more-cycles")
            self._say(f"gaffer: granted {opts.more_cycles} more repair cycle(s); limit is now {job.cycle_limit} (R8).")

        if job.state == "BLOCKED":
            job = self._rerun_blocked(job, repo, opts)
        elif job.state == "READY" and opts.cont:
            store.log_event(job.id, "continue", {"head": job.head_sha})
            job = store.set_state(job.id, "ROUND")
        return self._drive(job, repo)

    def _check_flags(self, job: st.Job | None, opts: Options) -> None:
        if opts.fresh and (opts.cont or opts.adopt_head):
            raise Refused("--fresh starts a new job; it can't be combined with --continue or --adopt-head.")
        if opts.fresh and job is not None and job.state != "CLOSED":
            raise Refused(f"--fresh archives only a CLOSED job (R8); {job_marker(job)} is {job.state}. "
                          f"Close its PR first, or rerun without --fresh.")
        if opts.cont and (job is None or job.state != "READY"):
            raise Refused(f"--continue processes feedback after READY (R8); "
                          f"{'there is no job' if job is None else f'{job_marker(job)} is {job.state}'}.")
        if opts.adopt_head:
            if job is None or job.state != "BLOCKED" or job.blocked_reason not in ("foreign_head", "base_conflict", "state_lost"):
                raise Refused("--adopt-head applies to a job BLOCKED by foreign_head or base_conflict (R8)"
                              + ("" if job is None else f"; {job_marker(job)} is {job.state}"
                                 + (f": {job.blocked_reason}" if job.blocked_reason else "")) + ".")
            if job.blocked_reason == "state_lost":
                raise Refused("rebuilding a lost job with --adopt-head (R9) isn't built yet: it comes after the "
                              "spike in build order step 5, and R23 (what evidence it may trust) is unresolved. "
                              "Close the PR and rerun with --fresh, or restore ~/.gaffer/state.db.")

    def _rerun_blocked(self, job: st.Job, repo: RepoConfig, opts: Options) -> st.Job:
        reason = job.blocked_reason or ""
        if reason == "state_lost":
            raise Blocked("state_lost", job.blocked_message or "state lost", step=None)
        if opts.adopt_head:
            return self._adopt_head(job, repo)
        step = RERUN_STEP.get(reason) or job.blocked_step or "PREFLIGHT"
        self.store.log_event(job.id, "rerun", {"from": reason, "step": step})
        return self.store.set_state(job.id, step)

    def _new_job(self, key: str, repo: RepoConfig) -> st.Job:
        store = self.store
        remote = self._remote_branches(key, repo)
        lost = store.check_state_lost(key, remote)
        if lost is not None:
            return store.record_state_lost(lost, team_key=repo.team_key, repo=repo.github)
        job = store.create_job(key, team_key=repo.team_key, repo=repo.github, state="INTAKE")
        self._say(f"gaffer: new job {job_marker(job)} on branch {job.branch}.")
        return job

    # -- driving ------------------------------------------------------------------

    def _drive(self, job: st.Job, repo: RepoConfig) -> int:
        handlers: dict[str, Callable[[st.Job, RepoConfig], st.Job]] = {
            "PREFLIGHT": lambda j, r: self.store.set_state(j.id, "INTAKE"),
            "INTAKE": self._intake,
            "WORKSPACE": self._workspace,
            "TESTER": self._tester,
            "RED_FIRST": self._tester,  # no recorded tester commit: rerun the tester (recovery table)
            "WRITER": self._writer,
            "VERIFY": self._verify,
            "PUBLISH": self._publish,
            "ROUND": self._round,
            "PACKET": self._packet,
            "RESUME": self._resume_step,
        }
        try:
            while job.state not in st.TERMINAL_STATES:
                self._say(f"gaffer: {job_marker(job)}: {job.state}")
                job = handlers[job.state](job, repo)
        except Blocked as b:
            job = self.store.get_job(job.id)
            step = b.step or (job.state if job.state in st.STEPS else None)
            if b.detail is not None:
                self.store.log_event(job.id, "blocked_detail", {"reason": b.reason, "detail": b.detail})
            if (rnd := self.store.open_round_for(job.id)) is not None:
                # foreign_head / base_conflict end the round; a rerun opens a fresh one on the (adopted) head.
                self.store.close_round(rnd.id, outcome="superseded")
            job = self.store.block(job.id, b.reason, b.message, step=step)
        except Closed as c:
            job = self._close(job, str(c))
        except ws_mod.SecretDetected as e:
            job = self.store.block(job.id, "secret_detected", str(e))
        except rv_packet.SecretDetected as e:
            job = self.store.block(job.id, "secret_detected", str(e))
        except ws_mod.SecretScanError as e:
            job = self.store.block(job.id, ws_mod.SECRET_SCAN_FAILED, str(e))
        except (Refused, ch.ChildError, gconfig.KeychainError, gconfig.ConfigError):
            raise
        except ws_mod.WorkspaceError as e:
            if e.reason is None:
                return self._internal_error(job, e)
            job = self.store.block(job.id, e.reason, str(e))
        except Exception as e:  # internal error: the job stays at its step; a rerun resumes it
            return self._internal_error(job, e)
        return self._finish(job)

    def _internal_error(self, job: st.Job, e: Exception) -> int:
        self._say(f"gaffer: error during {self.store.get_job(job.id).state}: {type(e).__name__}: {e}")
        self._say(f"gaffer: the job is unchanged; rerun `gaffer run {job.issue_key}` to continue from this step.")
        return EXIT_ERROR

    def _finish(self, job: st.Job) -> int:
        job = self.store.get_job(job.id)
        self._print_report(job, regenerate=True)
        return {"READY": EXIT_READY, "BLOCKED": EXIT_BLOCKED, "CLOSED": EXIT_CLOSED}.get(job.state, EXIT_ERROR)

    def _close(self, job: st.Job, why: str) -> st.Job:
        rnd = self.store.open_round_for(job.id)
        if rnd is not None:
            self.store.close_round(rnd.id, outcome="closed")
        self.store.log_event(job.id, "closed", {"why": why})
        return self.store.set_state(job.id, "CLOSED")

    # -- INTAKE -------------------------------------------------------------------

    def _intake(self, job: st.Job, repo: RepoConfig) -> st.Job:
        issue = self.d.linear().fetch_issue(job.issue_key)
        self.store.log_event(job.id, "intake", issue.to_dict())
        if issue.acceptance_criteria is None:
            raise Blocked("acceptance_criteria_missing",
                          f"{job.issue_key} has no \"Acceptance criteria\" heading with content and no checklist.", step="INTAKE")
        return self.store.set_state(job.id, "WORKSPACE")

    def _issue(self, job: st.Job) -> Issue:
        ev = self.store.events(job.id, "intake")
        if not ev:
            raise RuntimeError("no intake record for this job")
        return Issue.from_dict(ev[-1].detail)

    # -- WORKSPACE ----------------------------------------------------------------

    def _workspace(self, job: st.Job, repo: RepoConfig) -> st.Job:
        ws = self._load_ws(job)
        if ws is None:
            root = ws_mod.job_dir(self.cfg, job_marker(job))
            if root.exists() or root.is_symlink():
                # T7 report item 11: a create that failed part-way leaves the dir behind.
                self.store.log_event(job.id, "job_dir_removed", {"path": str(root)})
                if root.is_symlink():
                    root.unlink()
                else:
                    shutil.rmtree(root)
            ws = ws_mod.create_workspace(self.cfg, repo, job_marker(job), job.branch, token=self._git_token(),
                                         remote_url=self._remote_url(repo))
            job = self.store.set_workspace(job.id, clone_path=ws.clone, base_sha=ws.base_sha)
            self._save_ws(job, ws)
        if repo.install_command and not self.store.events(job.id, "install_done"):
            res = ws_mod.run_install(ws, repo, ws.clone, self._install_runner(job, ws, "WORKSPACE"))
            self._flush_restores(job, ws)
            if res is not None and not res.ok:
                raise Blocked("install_failed", f"install_command exited {res.exit_code} in the job clone: "
                              f"{res.output_tail[-1500:]}")
            self.store.log_event(job.id, "install_done", {"checkout": "clone"})
        return self.store.set_state(job.id, "TESTER")

    def _load_ws(self, job: st.Job) -> ws_mod.Workspace | None:
        if job.id in self._ws:
            return self._ws[job.id]
        ev = self.store.events(job.id, "workspace")
        if not ev or not job.clone_path or not Path(job.clone_path).exists():
            return None
        try:
            ws = ws_mod.Workspace.from_record(ev[-1].detail)
        except ws_mod.WorkspaceTampered as e:
            raise Blocked(ws_mod.WORKSPACE_TAMPERED, str(e)) from None
        self._ws[job.id] = ws
        self._flush_restores(job, ws)
        return ws

    def _ws_of(self, job: st.Job) -> ws_mod.Workspace:
        ws = self._load_ws(job)
        if ws is None:
            raise RuntimeError(f"{job_marker(job)} has no job clone; rerun from WORKSPACE")
        return ws

    def _save_ws(self, job: st.Job, ws: ws_mod.Workspace) -> None:
        self._ws[job.id] = ws
        self.store.log_event(job.id, "workspace", ws.to_record())
        self._flush_restores(job, ws)

    def _flush_restores(self, job: st.Job, ws: ws_mod.Workspace) -> None:
        """R31: every `.git/config` restore workspace made goes into the job log for the report."""
        while ws.config_restores:
            self.store.log_event(job.id, "git_config_restored", {"note": ws.config_restores.pop(0)})

    def _jobs(self, job: st.Job, ws: ws_mod.Workspace) -> ch.JobDirs:
        state = gaffer_dir(self.d.home) / "jobs" / job_marker(job)
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        return ch.JobDirs(job_dir=ws.root, clone=ws.clone, state_dir=state)

    def _state_dir(self, job: st.Job) -> Path:
        return gaffer_dir(self.d.home) / "jobs" / job_marker(job)

    def _install_runner(self, job: st.Job, ws: ws_mod.Workspace, step: str) -> ws_mod.InstallRunner:
        """ADAPTER: workspace's InstallRunner over children.run_install_command.
        children builds its own environment, so InstallRequest.env (TMPDIR, the
        R29 Composer vars, npm_config_cache) isn't passed through."""
        repo = self.cfg.repos[job.team_key]

        def run(req: ws_mod.InstallRequest) -> ws_mod.InstallResult:
            out = self._child(job, "install", step, None, lambda on_spawn: self.children.run_install_command(
                repo, self._jobs(job, ws), req.cwd, hosts=tuple(req.allowed_hosts), on_spawn=on_spawn))
            code = 1 if out.timed_out or out.exit_code is None else out.exit_code
            return ws_mod.InstallResult(code, (out.output_tail + out.stderr_tail)[-4000:])

        return run

    def _remote_url(self, repo: RepoConfig) -> str:
        return self.d.remote_url(repo) if self.d.remote_url else ws_mod.github_remote_url(repo.github)

    def _git_token(self) -> Secret | None:
        if self._token is False:
            self._token = self.d.token()
        return self._token  # type: ignore[return-value]

    def _git_auth(self, url: str) -> tuple[dict[str, str], tuple[str, ...]]:
        # workspace._auth_env is private; the runner needs it for ls-remote and the --adopt-head fetch.
        return ws_mod._auth_env(url, self._git_token())

    def _remote_branches(self, key: str, repo: RepoConfig) -> list[st.RemoteBranch]:
        """R9 state_lost: gaffer branches for this issue that exist on the remote."""
        url = self._remote_url(repo)
        env, secrets = self._git_auth(url)
        out = ws_mod.git(Path(self.cfg.job_root if self.cfg.job_root.exists() else "/"), "ls-remote", "--refs", url,
                         f"refs/heads/gaffer/{key}*", env=env, secrets=secrets).stdout.decode(errors="replace")
        branches = []
        for line in out.splitlines():
            _, _, ref = line.partition("\t")
            name = ref.removeprefix("refs/heads/")
            if st.branch_generation(key, name) is None:
                continue
            pr = None
            with contextlib.suppress(ws_mod.GitHubError, rv_github.GitHubError):
                pulls = self._github(repo).client.open_pulls_for_branch(name)
                pr = pulls[0]["number"] if pulls else None
            branches.append(st.RemoteBranch(name, pr))
        return branches

    def _github(self, repo: RepoConfig) -> GitHubPorts:
        if repo.team_key not in self._gh:
            self._gh[repo.team_key] = self.d.github(repo)
        return self._gh[repo.team_key]

    # -- children: attempts and leases (R9) ---------------------------------------

    def _child(self, job: st.Job, role: str, step: str, session_id: str | None, fn: Callable[[OnSpawn], Any]) -> Any:
        store = self.store
        store_role = _STORE_ROLE[role]
        attempt = store.start_attempt(job.id, role=store_role, step=step, session_id=session_id)
        leases: list[st.Lease] = []

        def on_spawn(lease: ch.ChildLease) -> None:
            rec = store.acquire_lease(job.id, role=store_role, pid=lease.pid, proc_start=lease.start_time or "unknown",
                                      pgid=lease.pgid, session_id=lease.session_id, attempt_id=attempt.id)
            leases.append(rec)
            store.log_event(job.id, "spawned", {"role": role, "session_id": lease.session_id, "pid": lease.pid,
                                                "pgid": lease.pgid})

        try:
            result = fn(on_spawn)
        except ch.InitCheckFailed:
            store.finish_attempt(attempt.id, outcome="init_check_failed")
            for lease in leases:
                store.release_lease(lease.id, "killed at init")
            raise
        except Exception as e:
            store.finish_attempt(attempt.id, outcome=f"error: {type(e).__name__}")
            for lease in leases:
                store.release_lease(lease.id, "exited")
            raise
        store.finish_attempt(attempt.id, outcome=_outcome(result), usage=_usage(result))
        for lease in leases:
            store.release_lease(lease.id, "exited")
        return result

    def _reap_leases(self, job: st.Job | None) -> None:
        """R9: release leases of dead children; refuse to act while any child of the job is alive."""
        if job is None:
            return
        alive, dead = self.store.alive_leases(job.id, _ProbeAdapter(self.children))
        for lease in dead:
            # The leader is gone, but its process group may not be (R9: wait up to 2 min, then SIGTERM).
            if self.children.group_alive(lease.pgid):
                if not self.children.wait_group(lease.pgid, WRITER_GROUP_GRACE):
                    self.children.terminate_group(lease.pgid)
                    self.store.log_event(job.id, f"{_role_name(lease.role).replace(' ', '_')}_group_sigterm",
                                         {"pgid": lease.pgid, "pid": lease.pid, "after": "rerun"})
        if alive:
            desc = ", ".join(f"{_role_name(l.role)} pid {l.pid} (process group {l.pgid})" for l in alive)
            raise Refused(f"{job_marker(job)} still has a live child: {desc}. gaffer won't start a second one (R9). "
                          f"Wait for it to exit, or kill its process group (`kill -TERM -<pgid>`); "
                          f"`gaffer stop` is a TODO (T1).")

    def _ensure_writer_gone(self, job: st.Job) -> None:
        """R9 / SC3: no writer is alive during ROUND. Wait up to 2 min for any writer
        process group, then SIGTERM it, recorded."""
        for lease in self.store.live_leases(job.id, role="writer"):
            if self.children.group_alive(lease.pgid):
                if not self.children.wait_group(lease.pgid, WRITER_GROUP_GRACE):
                    self.children.terminate_group(lease.pgid)
                    self.store.log_event(job.id, "writer_group_sigterm", {"pgid": lease.pgid, "pid": lease.pid})
                self.store.release_lease(lease.id, "sigterm")
            else:
                self.store.release_lease(lease.id, "exited")

    # -- TESTER and RED-FIRST -----------------------------------------------------

    def _tester(self, job: st.Job, repo: RepoConfig) -> st.Job:
        store = self.store
        ws = self._ws_of(job)
        if job.tester_commit_sha:
            # Recovery table: the tester commit is on the branch (recorded only after red-first passed): keep it.
            if ws.tester_commit != job.tester_commit_sha:
                ws.tester_commit = job.tester_commit_sha
                self._save_ws(job, ws)
            return store.set_state(job.id, "WRITER")
        self._reset_to_base(job, ws)
        issue = self._issue(job)
        retry: str | None = None
        for attempt in (1, 2):
            job = store.set_state(job.id, "TESTER")
            prompt = ch.tester_prompt(repo, issue_key=job.issue_key, issue_title=issue.title,
                                      issue_body=issue.description, retry_error=retry)
            run: ch.TesterRun = self._child(job, "tester", "TESTER", None,
                                            lambda on_spawn: self.children.run_tester(self._jobs(job, ws), prompt, on_spawn=on_spawn))
            if run.run.limit is not None:
                raise Blocked("usage_limit", _limit_text(run.run.limit))
            if run.result is None:
                invalid = run.run.data is not None  # output arrived but failed validation
                if attempt == 1:
                    retry = run.result_error
                    continue
                raise Blocked("tester_invalid" if invalid else "tester_failed",
                              f"after one retry: {run.result_error}")
            tr = run.result
            if not tr.testable_criteria:
                raise Blocked("no_testable_criteria", "the tester judged every criterion untestable: "
                              + "; ".join(f"{c}: {r}" for c, r in tr.untestable))
            try:
                write = ws_mod.write_tester_files(ws, repo, [ws_mod.TesterFile(p, c) for p, c in tr.files])
            except ws_mod.TesterOutputInvalid as e:
                self._reset_to_base(job, ws)
                if attempt == 1:
                    retry = str(e)
                    continue
                raise Blocked("tester_invalid", f"after one retry: {e}") from None
            job = store.set_state(job.id, "RED_FIRST")
            commit = ws_mod.commit_tester_files(ws, write, f"{job.issue_key}: tester tests\n\nWritten by the Codex tester; "
                                                f"red on base {job.base_sha} (gaffer red-first).\n")
            cmap_doc = _criterion_doc(tr, write)
            result = self._red_first(job, ws, repo, cmap_doc, commit)
            if result.status is rv_redfirst.RedFirstStatus.OK:
                job = store.set_tester_commit(job.id, commit, criterion_map=cmap_doc)
                self._save_ws(job, ws)
                return store.set_state(job.id, "WRITER")
            self._reset_to_base(job, ws)
            if result.status is rv_redfirst.RedFirstStatus.ALREADY_PASSING:
                lst = "; ".join(f"{v.test.identity} (criterion {v.test.criterion_id})" for v in result.already_passing)
                raise Blocked("already_passing", f"acceptance tests pass on the base: {lst}")
            if attempt == 1:
                retry = result.retry_feedback()
                continue
            raise Blocked("tester_invalid", "red-first failed after one retry:\n" + result.retry_feedback())
        raise AssertionError("unreachable")

    def _reset_to_base(self, job: st.Job, ws: ws_mod.Workspace) -> None:
        """Drop an unvalidated tester commit or stray files: the clone goes back to the base."""
        head = ws_mod._out(ws.clone, "rev-parse", "HEAD")
        dirty = ws_mod._status_paths(ws.clone)
        if head == ws.base_sha and not dirty and ws.tester_commit is None:
            return
        ws_mod.git(ws.clone, "checkout", "--quiet", "-f", "-B", ws.branch, ws.base_sha)
        ws_mod.git(ws.clone, "clean", "-fdq")
        ws.tester_commit, ws.tester_files = None, ()
        self._save_ws(job, ws)

    def _red_first(self, job: st.Job, ws: ws_mod.Workspace, repo: RepoConfig, cmap_doc: Mapping[str, Any],
                   commit: str) -> rv_redfirst.RedFirstResult:
        run, out = self._test_run(job, ws, repo, commit, "RED_FIRST")
        cmap = _criterion_map(cmap_doc)
        result = rv_redfirst.classify_red_first(cmap, run)
        self.store.record_test_run(job.id, head_sha=job.base_sha, kind="red_first", results=_case_results(run, cmap),
                                   exit_code=out.exit_code if out else None, junit_found=run.usable)
        self.store.log_event(job.id, "red_first", {"status": result.status.value, "candidate": commit,
                                                   "verdicts": [{"test": v.test.identity, "verdict": v.verdict.value,
                                                                 "reason": v.reason[:500]} for v in result.verdicts],
                                                   "problems": list(result.problems)})
        return result

    def _test_run(self, job: st.Job, ws: ws_mod.Workspace, repo: RepoConfig, head: str,
                  step: str) -> tuple[rv_junit.RunResult, ch.CommandRun | None]:
        """gaffer's own test run on exactly `head` in a fresh verification worktree (R4)."""
        install = self._install_runner(job, ws, step) if repo.install_command else None
        vc = ws_mod.prepare_verification(ws, repo, head, install=install)
        self._flush_restores(job, ws)
        if vc.install is not None and not vc.install.ok:
            raise Blocked("install_failed", f"install_command exited {vc.install.exit_code} in the verification "
                          f"worktree: {vc.install.output_tail[-1500:]}")
        jobs = self._jobs(job, ws)
        if repo.runner == "phpunit":
            files = sorted({str(p.relative_to(vc.path)) for d in repo.test_dirs for p in (vc.path / d).rglob("*Test.php")})
            runs, out = [], None
            for f in files:
                junit = vc.path / repo.junit_path
                junit.unlink(missing_ok=True)
                cmd = rv_junit.phpunit_file_command(repo.test_command, f)
                out = self._child(job, "test", step, None, lambda on_spawn, cmd=cmd: self.children.run_test_command(
                    repo, jobs, vc.path, command=cmd, on_spawn=on_spawn))
                runs.append(rv_junit.PhpunitFileRun(f, junit.read_bytes() if junit.is_file() else None,
                                                    out.output_tail + out.stderr_tail))
            return rv_junit.parse_phpunit_runs(runs, root=str(vc.path)), out
        junit = vc.path / repo.junit_path
        junit.unlink(missing_ok=True)
        env = rv_junit.runner_env(repo.runner, repo.junit_path)
        prefix = "".join(f"export {k}={shlex.quote(v)}; " for k, v in env.items())
        out = self._child(job, "test", step, None, lambda on_spawn: self.children.run_test_command(
            repo, jobs, vc.path, command=prefix + repo.test_command, on_spawn=on_spawn))
        data = junit.read_bytes() if junit.is_file() else None
        root = vc.path
        return rv_junit.parse_junit(repo.runner, data, root=str(root), exists=lambda p: (root / p).is_file()), out

    # -- WRITER -------------------------------------------------------------------

    def _writer(self, job: st.Job, repo: RepoConfig) -> st.Job:
        ws = self._ws_of(job)
        if not job.tester_commit_sha:
            return self.store.set_state(job.id, "TESTER")
        if ws.tester_commit is None:
            ws.tester_commit = job.tester_commit_sha
            self._save_ws(job, ws)
        session = job.writer_session_id
        if session and self._spawned(job, session):
            # Recovery table, WRITER (child dead): resume the same session with "continue".
            self.store.log_event(job.id, "writer_continue", {"session_id": session})
            run = self._resume(job, ws, repo, session, ch.CONTINUE_PROMPT, item_ids=None, step="WRITER")
        else:
            if not session:
                session = ch.new_session_id()
                job = self.store.set_writer_session(job.id, session)  # R9: recorded before launch
            issue = self._issue(job)
            tr = _tester_result(job.criterion_map)
            prompt = ch.implement_prompt(repo, self._jobs(job, ws), issue_key=job.issue_key, issue_title=issue.title,
                                         issue_body=issue.description, tester=tr, tester_commit=job.tester_commit_sha,
                                         base_sha=job.base_sha)
            run = self._launch(job, ws, repo, session, prompt, item_ids=None, step="WRITER")
        self._finish_writer(job, ws, repo, run, previous_head=job.tester_commit_sha, item_ids=None, step="WRITER")
        return self.store.set_state(job.id, "VERIFY")

    def _spawned(self, job: st.Job, session: str) -> bool:
        return any((e.detail or {}).get("session_id") == session and (e.detail or {}).get("role") == "writer"
                   for e in self.store.events(job.id, "spawned"))

    def _blocked_plugins(self) -> list[str]:
        """R31: the persistent plugin block list under ~/.gaffer. children.py takes the
        keys per launch (`plugin_keys=`) but doesn't persist them, so the runner does."""
        path = gaffer_dir(self.d.home) / PLUGIN_BLOCKLIST
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return []
        except ValueError:
            raise Refused(f"{path} is not valid JSON; fix or remove it.") from None
        keys = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise Refused(f"{path} must be {{\"plugins\": [\"<key>\", ...]}}.")
        return keys

    def _block_plugins(self, keys: Sequence[str]) -> None:
        path = gaffer_dir(self.d.home) / PLUGIN_BLOCKLIST
        merged = list(dict.fromkeys([*self._blocked_plugins(), *keys]))
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"plugins": merged}, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _init_failed(self, job: st.Job, e: ch.InitCheckFailed, attempt: int, *, what: str) -> list[str]:
        """R31: returns the newly learned plugin keys to block before the one relaunch;
        raises BLOCKED: isolation_failed for anything else or a second failure."""
        plugins = ch.plugin_keys_from_init(e.init or {})
        other = [p for p in e.problems if not p.startswith("plugins loaded")]
        new = [p for p in plugins if p not in self._blocked_plugins()]
        self.store.log_event(job.id, "init_check_failed", {"what": what, "problems": list(e.problems), "plugins": plugins,
                                                           "attempt": attempt})
        if attempt >= 2 or other or not new:
            raise Blocked("isolation_failed", f"{what} was killed at its init event"
                          + (" again after one relaunch" if attempt >= 2 else "") + ": " + "; ".join(e.problems))
        self._block_plugins(new)
        self.store.log_event(job.id, "plugins_blocked", {"plugins": new})
        return new

    def _launch(self, job: st.Job, ws: ws_mod.Workspace, repo: RepoConfig, session: str, prompt: str, *,
                item_ids: Sequence[str] | None, step: str) -> ch.WriterRun:
        for attempt in (1, 2):
            try:
                return self._child(job, "writer", step, session, lambda on_spawn: self.children.launch_writer(
                    repo, self._jobs(job, ws), prompt, session_id=session, item_ids=item_ids, on_spawn=on_spawn,
                    plugin_keys=self._blocked_plugins()))
            except ch.InitCheckFailed as e:
                self._init_failed(job, e, attempt, what="the writer")
                # The killed child may already have created the session: relaunch with a fresh ID (T5).
                session = ch.new_session_id()
                job = self.store.set_writer_session(job.id, session)
        raise AssertionError("unreachable")

    def _resume(self, job: st.Job, ws: ws_mod.Workspace, repo: RepoConfig, session: str, prompt: str, *,
                item_ids: Sequence[str] | None, step: str, handoff: str = "") -> ch.WriterRun:
        for attempt in (1, 2):
            try:
                run = self._child(job, "writer", step, session, lambda on_spawn: self.children.resume_writer(
                    repo, self._jobs(job, ws), session, prompt, item_ids=item_ids, on_spawn=on_spawn,
                    plugin_keys=self._blocked_plugins()))
                break
            except ch.InitCheckFailed as e:
                # A resume keeps its session: the session already exists, and the child died before acting.
                self._init_failed(job, e, attempt, what="the resumed writer")
        if run.init is None and run.limit is None and not run.timed_out:
            # Design "Session resume": the session couldn't be resumed. Start a fresh writer
            # seeded with a structured handoff, and say so in the report.
            new = ch.new_session_id()
            job = self.store.set_writer_session(job.id, new)
            self.store.log_event(job.id, "fresh_writer", {"old_session": session, "new_session": new,
                                                          "why": run.result_error or "resume failed"})
            text = self._handoff(job, ws, repo, prompt)
            return self._launch(job, ws, repo, new, text, item_ids=item_ids, step=step)
        return run

    def _handoff(self, job: st.Job, ws: ws_mod.Workspace, repo: RepoConfig, task: str) -> str:
        issue = self._issue(job)
        tr = _tester_result(job.criterion_map)
        base = ch.implement_prompt(repo, self._jobs(job, ws), issue_key=job.issue_key, issue_title=issue.title,
                                   issue_body=issue.description, tester=tr, tester_commit=job.tester_commit_sha,
                                   base_sha=job.base_sha)
        stat = ws_mod._out(ws.clone, "diff", "--stat", f"{job.base_sha}..HEAD")
        log = ws_mod._out(ws.clone, "log", "--oneline", f"{job.base_sha}..HEAD")
        return (f"{base}\n\n## Handoff (fresh session)\n\nA previous writer session for this job couldn't be resumed. "
                f"The branch already holds this work:\n\n```\n{log}\n\n{stat}\n```\n\n"
                f"Continue from there. The current task is below.\n\n{task}")

    def _check_writer_run(self, run: ch.WriterRun) -> ch.WriterResult:
        if run.limit is not None:
            raise Blocked("usage_limit", _limit_text(run.limit))
        if run.result is None:
            raise Blocked("writer_no_result", run.result_error or "the writer returned no structured result")
        if run.result.status == "blocked":
            raise Blocked("writer_blocked", f"the writer asked: {run.result.question}", detail={"question": run.result.question})
        return run.result

    def _finish_writer(self, job: st.Job, ws: ws_mod.Workspace, repo: RepoConfig, run: ch.WriterRun, *,
                       previous_head: str, item_ids: Sequence[str] | None, step: str) -> _Writer:
        """R10 gates after every WRITER and RESUME, with one nudge for a dirty tree."""
        job = self.store.get_job(job.id)
        self._writer_facts(job, run)
        result = self._check_writer_run(run)
        nudged = False
        while True:
            cited = [d.commit_sha for d in result.dispositions if d.disposition == "fixed" and d.commit_sha]
            require_new = item_ids is None or any(d.disposition == "fixed" for d in result.dispositions)
            gate = ws_mod.check_writer_result(ws, repo, previous_head=previous_head, cited_shas=cited, nudged=nudged,
                                              require_new_commit=require_new)
            self._flush_restores(job, ws)
            if gate.action is ws_mod.GateAction.NUDGE:
                self.store.log_event(job.id, "writer_nudge", {"dirty": list(gate.dirty[:50])})
                nudged = True
                run = self._resume(job, ws, repo, self.store.get_job(job.id).writer_session_id, ch.NUDGE_PROMPT,
                                   item_ids=item_ids, step=step)
                self._writer_facts(job, run)
                result = self._check_writer_run(run)
                continue
            break
        if gate.guarded_changes:
            self.store.log_event(job.id, "guarded_changes", {"changes": [asdict(g) for g in gate.guarded_changes]})
        if gate.action is ws_mod.GateAction.BLOCKED:
            raise Blocked(gate.reason or "writer_no_result", gate.message)
        return _Writer(result, gate, run.session_id)

    def _writer_facts(self, job: st.Job, run: ch.WriterRun) -> None:
        self.store.log_event(job.id, "writer_run", {"session_id": run.session_id, "skills": list(run.skills),
                                                    "model": run.model, "claude_version": run.claude_version,
                                                    "group_terminated": run.group_terminated,
                                                    "summary": run.result.summary if run.result else None})
        if run.group_terminated:
            self.store.log_event(job.id, "writer_group_sigterm", {"pid": run.lease.pid, "pgid": run.lease.pgid})

    # -- VERIFY (R4, R24) ---------------------------------------------------------

    def _verify(self, job: st.Job, repo: RepoConfig) -> st.Job:
        ws = self._ws_of(job)
        head = self._local_head(ws)
        run, out = self._test_run(job, ws, repo, head, "VERIFY")
        cmap = _criterion_map(job.criterion_map)
        inv = rv_junit.match_inventory(cmap, run)
        regressions = self._regressions(job, run, cmap)
        self.store.record_test_run(job.id, head_sha=head, kind="verify", results=_case_results(run, cmap, inv),
                                   exit_code=out.exit_code if out else None, junit_found=run.usable)
        passed = inv.all_passed and not regressions
        self.store.log_event(job.id, "verify", {
            "head": head, "passed": passed, "notes": list(run.notes) + list(inv.notes), "problems": list(run.problems),
            "inventory": [{"criterion": e.test.criterion_id, "label": e.test.label.value, "test": e.test.identity,
                           "outcome": e.outcome.value} for e in inv.entries],
            "regressions": [e.test.identity for e in regressions]})
        if passed:
            return self.store.set_state(job.id, "PUBLISH")
        # R24: a local packet resumes the same session and counts as a repair cycle; nothing is published.
        failing = list(inv.failing) + regressions
        pkt = rv_packet.build_packet(head_sha=head, seq=0, scanner=PacketScanner(self.d.scanner), failing_tests=failing)
        by_identity: dict[str, int] = {}
        for entry, text in ((e, rv_packet.render_failing_test(e)) for e in failing):
            item, _ = self.store.add_item(job.id, source="test", item_id=f"{head}:{entry.test.identity}",
                                          content_hash=_sha256(text), head_sha=head,
                                          payload={"kind": "failing_test", "test": entry.test.identity,
                                                   "criterion": entry.test.criterion_id, "outcome": entry.outcome.value,
                                                   "text": text})
            by_identity[entry.test.identity] = item.id
        ids = [by_identity[k[1]] for k in pkt.included_keys if k[1] in by_identity]
        ids = [i for i in dict.fromkeys(ids) if self.store.get_item(i).packet_id is None]
        if not ids:
            raise Blocked("verify_failed", f"gaffer's test run on {_short(head)} failed and no new item could be "
                          f"packaged: {', '.join(e.test.identity for e in failing)[:1500]}")
        try:
            self.store.create_packet(job.id, kind="local", head_sha=head, item_ids=ids,
                                     content={"text": pkt.text, "keys": [list(k) for k in pkt.included_keys]})
        except st.RepairLimitReached as e:
            raise Blocked("verify_failed", f"gaffer's test run on {_short(head)} still fails "
                          f"({', '.join(x.test.identity for x in failing)[:1500]}) and {e}") from None
        return self.store.set_state(job.id, "RESUME")

    def _regressions(self, job: st.Job, run: rv_junit.RunResult, cmap: rv_junit.CriterionMap) -> list[rv_junit.InventoryEntry]:
        """Non-tester cases that fail now but passed (or didn't exist) on the base (R4:
        base failures are recorded separately and don't block READY)."""
        if not run.usable:
            return []
        base = self.store.latest_test_run(job.id, head_sha=job.base_sha, kind="red_first")
        base_failing = set()
        if base is not None:
            base_failing = {(r.classname, r.name) for r in self.store.test_results(base.id) if r.outcome != "passed"}
        tester = {(t.classname, t.name) for t in cmap.tests}
        out = []
        for c in run.cases:
            if c.passed or (c.classname, c.name) in tester or (c.classname, c.name) in base_failing:
                continue
            if c.collection_error and any(run.collection_error_for(t.file) is c for t in cmap.tests):
                continue
            t = rv_junit.ExpectedTest("(suite)", rv_junit.Label.GUARD, c.file, c.classname, c.name)
            out.append(rv_junit.InventoryEntry(t, c.outcome, case=c, detail=f"{c.outcome.value}: {c.summary or c.exc_type}"))
        return out

    def _local_head(self, ws: ws_mod.Workspace) -> str:
        head = ws_mod._resolve_commit(ws.clone, f"refs/heads/{ws.branch}")
        if head is None:
            raise Blocked(ws_mod.WRITER_BRANCH_MOVED, f"refs/heads/{ws.branch} does not resolve in the job clone")
        return head

    def _verified(self, job: st.Job, head: str) -> bool:
        return any((e.detail or {}).get("head") == head and (e.detail or {}).get("passed")
                   for e in self.store.events(job.id, "verify"))

    # -- PUBLISH ------------------------------------------------------------------

    def _publish(self, job: st.Job, repo: RepoConfig) -> st.Job:
        ws = self._ws_of(job)
        head = self._local_head(ws)
        if not self._verified(job, head):
            return self.store.set_state(job.id, "VERIFY")  # R24: never publish a head gaffer hasn't verified
        gh = self._github(repo)
        if job.pr_number is not None:
            self._check_pull(job, gh.reader.pull(repo.github, job.pr_number), head=None)
        try:
            res = ws_mod.push(ws, head, scanner=self.d.scanner, expected_remote=job.head_sha, token=self._git_token())
        except ws_mod.PushRejected as e:
            raise Blocked("foreign_head", f"{e}. Rerun with --adopt-head to accept the remote head (R8).",
                          step="PUBLISH") from None
        self._flush_restores(job, ws)
        self.store.log_event(job.id, "push", {"head": head, "pushed": res.pushed, "remote_before": res.remote_before})
        job = self.store.set_head(job.id, head)
        if job.pr_number is None:
            issue = self._issue(job)
            tr = _tester_result(job.criterion_map)
            rows = [ws_mod.CriterionRow(f"{cid}: {text}", tuple(f"{t.identity.file}::{t.identity.classname}::{t.identity.name}"
                                                                 f" ({t.label})" for t in tr.tests if t.criterion_id == cid))
                    for cid, text in tr.criteria]
            body = ws_mod.pr_body(job_marker(job), rows, [f"{c}: {r}" for c, r in tr.untestable])
            pr = ws_mod.open_pull_request(gh.client, ws, title=ws_mod.pr_title(job.issue_key, issue.title), body=body,
                                          scanner=self.d.scanner)
            job = self.store.set_pr(job.id, pr.number)
            self.store.log_event(job.id, "pr", {"number": pr.number, "url": pr.url, "created": pr.created})
        self._codex_review(job, ws, repo, head)
        self._ensure_writer_gone(job)
        return self.store.set_state(job.id, "ROUND")

    def _codex_review(self, job: st.Job, ws: ws_mod.Workspace, repo: RepoConfig, head: str) -> None:
        """One Codex review per head, recorded; a rerun reuses it (design, recovery table ROUND)."""
        if self.store.review_for_head(job.id, head) is not None:
            return
        issue = self._issue(job)
        tr = _tester_result(job.criterion_map)
        changes = [(c["path"], c.get("reason") or "(no reason)") for e in self.store.events(job.id, "guarded_changes")
                   for c in e.detail["changes"]]
        prompt = ch.review_prompt(repo, issue_key=job.issue_key, issue_title=issue.title, issue_body=issue.description,
                                  tester=tr, base_sha=job.base_sha, head_sha=head, harness_changes=list(dict.fromkeys(changes)))
        last_error = ""
        for _attempt in (1, 2):
            run: ch.ReviewRun = self._child(job, "reviewer", "PUBLISH", None, lambda on_spawn: self.children.run_reviewer(
                self._jobs(job, ws), prompt, on_spawn=on_spawn))
            if run.run.limit is not None:
                raise Blocked("usage_limit", _limit_text(run.run.limit))
            if run.result is not None:
                break
            last_error = run.result_error or "no result"
        else:
            raise Blocked("reviewer_failed", f"the Codex review of {_short(head)} failed twice: {last_error}")
        r = run.result
        doc = {"summary": r.summary, "findings": [asdict(f) for f in r.findings],
               "harness_judgements": [asdict(j) for j in r.harness_judgements]}
        self.store.record_review(job.id, head_sha=head, result=doc)
        # R31: Codex review findings are packet items, admitted alongside CodeRabbit items and failed checks.
        for f in r.findings:
            body = f"[{f.severity}] {f.title}\n\n{f.body}"
            self.store.add_item(job.id, source="codex", item_id=f"{head}:{f.id}", content_hash=rv_cr.content_hash(body),
                                head_sha=head, payload={"source": "codex", "item_id": f"{head}:{f.id}", "kind": "codex_finding",
                                                        "author": "codex", "body": rv_cr.normalize_body(body),
                                                        "path": f.path, "line_start": f.line, "line_end": f.line})

    # -- ROUND --------------------------------------------------------------------

    def _round(self, job: st.Job, repo: RepoConfig) -> st.Job:
        store = self.store
        head = job.head_sha
        if head is None or job.pr_number is None:
            return store.set_state(job.id, "PUBLISH")
        self._ensure_writer_gone(job)
        last = self._last_round(job, head)
        if last is not None and last.outcome == "complete" and not self._consumed(job, last):
            return store.set_state(job.id, "PACKET")  # crashed between round completion and PACKET
        gh = self._github(repo)
        row, _ = store.open_round(job.id, head_sha=head, deadline_seconds=repo.round_timeout)
        state = self._round_state(job, row)
        rnd = rv_rounds.Round(state, clock=self.d.clock, round_timeout=row.deadline_seconds)
        sup = store.supervise(row.id)
        persisted = json.dumps(_round_doc(state), sort_keys=True)
        while True:
            tick = sup.tick()
            out = obs = None
            try:
                rnd.tick()
                snap = rv_rounds.fetch_snapshot(gh.reader, repo.github, job.pr_number, head)
                self._check_pull(job, snap.pull, head=head)
                obs = rv_rounds.evaluate_snapshot(snap, repo=repo, operator_login=self.cfg.operator_login,
                                                  processed_keys=self._processed_keys(job),
                                                  seconds_since_push=rnd.state.seconds_since_push)
                out = rnd.observe(checks=obs.checks, codex_recorded=store.review_for_head(job.id, head) is not None,
                                  coderabbit=obs.coderabbit, pending_keys=obs.pending_keys)
            except (rv_github.GitHubError, ws_mod.GitHubError, rv_cr.IncompleteData) as e:
                # API errors never count as an empty or completed review (design).
                store.log_event(job.id, "poll_error", {"error": str(e)[:500]}, round_id=row.id)
            doc = json.dumps(_round_doc(rnd.state), sort_keys=True)
            if doc != persisted:
                store.log_event(job.id, "round_state", _round_doc(rnd.state), round_id=row.id)
                persisted = doc
            if out is not None and out.status is rv_rounds.RoundStatus.COMPLETE:
                return self._round_complete(job, repo, row, out, obs)
            if out is not None and out.status is rv_rounds.RoundStatus.TIMED_OUT or (out is None and tick.expired):
                store.close_round(row.id, outcome="timed_out", coderabbit=_cr_state(rnd.state),
                                  coderabbit_note=rnd.state.coderabbit_reason or None)
                waiting = ", ".join(out.waiting_on) if out else "GitHub reads kept failing"
                raise Blocked("review_timeout", f"round {row.seq} on {_short(head)} not complete after "
                              f"{int(row.deadline_seconds)} supervised seconds; still waiting on: {waiting}", step="ROUND")
            if out is not None and out.status is rv_rounds.RoundStatus.CHECK_MISSING:
                store.close_round(row.id, outcome="timed_out", coderabbit=_cr_state(rnd.state),
                                  coderabbit_note=rnd.state.coderabbit_reason or None)
                raise Blocked("required_check_missing", f"{out.reason} on {_short(head)}", step="ROUND")
            self.d.sleep(POLL_SECONDS + self.d.rng.uniform(0, POLL_JITTER))

    def _round_state(self, job: st.Job, row: st.Round) -> rv_rounds.RoundState:
        """ADAPTER: store.rounds keeps supervised_seconds (R25) but not the rest of
        reviews.RoundState (seen keys, settle anchors, CodeRabbit state), so the
        runner keeps that in the job log as `round_state` events."""
        evs = [e for e in self.store.events(job.id, "round_state") if e.round_id == row.id]
        if evs:
            state = rv_rounds.RoundState.from_dict(evs[-1].detail)
        else:
            prior = sum(r.supervised_seconds for r in self.store.rounds(job.id)
                        if r.head_sha == row.head_sha and r.id != row.id)
            state = rv_rounds.RoundState(head_sha=row.head_sha, seq=row.seq, since_push_at_open=prior,
                                         wall_opened_at=row.opened_at)
        state.supervised_seconds = row.supervised_seconds
        return state

    def _check_pull(self, job: st.Job, pull: Mapping[str, Any], *, head: str | None) -> None:
        if pull.get("merged") or pull.get("merged_at") or pull.get("state") == "closed":
            how = "merged" if (pull.get("merged") or pull.get("merged_at")) else "closed"
            raise Closed(f"PR #{job.pr_number} was {how} by someone else")
        remote = str((pull.get("head") or {}).get("sha") or "")
        if head is not None and remote and remote != head:
            raise Blocked("foreign_head", f"PR #{job.pr_number}'s head is {remote}, not {head}, which gaffer pushed. "
                          f"Rerun with --adopt-head to accept it (R8).", step="ROUND")
        if pull.get("mergeable") is False or pull.get("mergeable_state") == "dirty":
            raise Blocked("base_conflict", f"PR #{job.pr_number} conflicts with {pull.get('base', {}).get('ref', 'the base')}. "
                          f"Merge or rebase it yourself, push, and rerun with --adopt-head (R8).", step="ROUND")

    def _processed_keys(self, job: st.Job) -> set[Key]:
        """Items already in a packet: never re-applied (design, SC6)."""
        return {(i.source, i.item_id, i.content_hash) for i in self.store.items(job.id) if i.packet_id is not None}

    def _round_complete(self, job: st.Job, repo: RepoConfig, row: st.Round, out: rv_rounds.RoundOutcome,
                        obs: rv_rounds.Observation) -> st.Job:
        store = self.store
        admitted = {i.key: i for i in obs.admission.admitted}
        for key in out.packet_keys:
            it = admitted.get(key)
            if it is None:
                continue
            store.add_item(job.id, source=_RV_TO_STORE_SOURCE[str(it.key[0])], item_id=it.item_id,
                           content_hash=it.content_hash, head_sha=row.head_sha, round_id=row.id, payload=_item_doc(it))
        checks = out.checks or obs.checks
        failed = [v.check_run for v in checks.failed if v.check_run is not None]
        store.log_event(job.id, "round_outcome", {
            "round_id": row.id, "head": row.head_sha, "seq": row.seq,
            "checks": [{"name": v.name, "state": v.state.value, "reason": v.reason} for v in checks.verdicts],
            "checks_all_passed": checks.all_passed, "no_ci_evidence": checks.no_ci_evidence,
            "failed_check_runs": [_check_run_doc(c) for c in failed],
            "failed_statuses": [v.name for v in checks.failed if v.check_run is None],
            "coderabbit": obs.coderabbit.state.value, "coderabbit_reason": obs.coderabbit.reason,
            "ignored": [asdict(d) for d in obs.admission.ignored],
            "context": [asdict(d) for d in obs.admission.context],
            "not_reviewed_files": list(obs.admission.not_reviewed_files),
            "packet_keys": [list(k) for k in out.packet_keys],
        }, round_id=row.id)
        cr = "verified" if obs.coderabbit.verified else "unverified"
        store.close_round(row.id, outcome="complete", coderabbit=cr,
                          coderabbit_note=None if obs.coderabbit.verified else (obs.coderabbit.reason or "unverified"))
        return store.set_state(job.id, "PACKET")

    def _last_round(self, job: st.Job, head: str | None = None) -> st.Round | None:
        rounds = [r for r in self.store.rounds(job.id) if head is None or r.head_sha == head]
        return rounds[-1] if rounds else None

    def _consumed(self, job: st.Job, rnd: st.Round) -> bool:
        return any(p.round_id == rnd.id for p in self.store.packets(job.id)) or any(
            (e.detail or {}).get("round_id") == rnd.id for e in self.store.events(job.id, "round_consumed"))

    def _round_outcome(self, job: st.Job, rnd: st.Round) -> dict[str, Any]:
        for e in reversed(self.store.events(job.id, "round_outcome")):
            if e.round_id == rnd.id:
                return e.detail
        raise RuntimeError(f"no recorded outcome for round {rnd.id}")

    # -- PACKET -------------------------------------------------------------------

    def _packet(self, job: st.Job, repo: RepoConfig) -> st.Job:
        store = self.store
        rnd = self._last_round(job, job.head_sha)
        if rnd is None or rnd.outcome != "complete":
            return store.set_state(job.id, "ROUND")
        open_pkt = store.open_packet(job.id)
        if open_pkt is not None:
            return store.set_state(job.id, "RESUME")
        outcome = self._round_outcome(job, rnd)
        packet_keys = {tuple(k) for k in outcome.get("packet_keys", ())}
        items = [i for i in store.pending_items(job.id)
                 if (i.source in ("coderabbit", "operator") and (i.source, i.item_id, i.content_hash) in packet_keys)
                 or (i.source == "codex" and i.head_sha == rnd.head_sha)]
        gh = self._github(repo)
        evidence: list[rv_checks.CheckEvidence] = []
        check_items: dict[str, int] = {}
        for cr in outcome.get("failed_check_runs", ()):
            e = rv_checks.collect_check_evidence(gh.reader, repo.github, cr)
            text = rv_packet.render_check(e)
            item, _ = store.add_item(job.id, source="check", item_id=f"{rnd.head_sha}:{e.check_run_id}",
                                     content_hash=_sha256(text), head_sha=rnd.head_sha, round_id=rnd.id,
                                     payload={"kind": "failed_check", "name": e.name, "text": text})
            if item.packet_id is None:
                evidence.append(e)
                check_items[str(e.check_run_id)] = item.id
        if not items and not evidence:
            self._post_round_comment(job, repo, rnd, None)
            store.log_event(job.id, "round_consumed", {"round_id": rnd.id})
            return self._evaluate_ready(job, repo, rnd)
        fb = [_feedback_item(i) for i in items]
        pkt = rv_packet.build_packet(head_sha=rnd.head_sha, seq=rnd.seq, scanner=PacketScanner(self.d.scanner),
                                     items=fb, failed_checks=evidence)
        by_key = {(i.source, i.item_id, i.content_hash): i.id for i in items}
        ids: list[int] = []
        for k in pkt.included_keys:
            if k[0] == "ci":
                ids.append(check_items[k[1]])
            else:
                ids.append(by_key[(_RV_TO_STORE_SOURCE[k[0]], k[1], k[2])])
        try:
            store.create_packet(job.id, kind="round", head_sha=rnd.head_sha, round_id=rnd.id, item_ids=ids,
                                content={"text": pkt.text, "keys": [list(k) for k in pkt.included_keys],
                                         "omitted": [list(k) for k in pkt.omitted_keys]})
        except st.RepairLimitReached as e:
            raise Blocked("repair_limit", f"round {rnd.seq} on {_short(rnd.head_sha)} has {len(ids)} item(s) to fix "
                          f"and {e}", step="PACKET") from None
        return store.set_state(job.id, "RESUME")

    # -- RESUME -------------------------------------------------------------------

    def _resume_step(self, job: st.Job, repo: RepoConfig) -> st.Job:
        store = self.store
        ws = self._ws_of(job)
        packet = store.open_packet(job.id)
        if packet is None:
            done = store.packets(job.id)
            if not done:
                return store.set_state(job.id, "VERIFY")
            return self._after_resume(job, repo, done[-1])  # recovery table: result recorded, continue
        items = store.packet_items(packet.id)
        item_ids = [str(i.id) for i in items]
        prompt_items = [ch.PromptItem(str(i.id), _prompt_source(i), _prompt_location(i), _prompt_body(i)) for i in items]
        prompt = ch.address_prompt(repo, head_sha=packet.head_sha, items=prompt_items, cycle=packet.cycle,
                                   max_cycles=job.cycle_limit)
        session = job.writer_session_id
        store.mark_resume_started(packet.id)
        if session is None:
            session = ch.new_session_id()
            job = store.set_writer_session(job.id, session)
            store.log_event(job.id, "fresh_writer", {"old_session": None, "new_session": session, "why": "no session recorded"})
            run = self._launch(job, ws, repo, session, self._handoff(job, ws, repo, prompt), item_ids=item_ids, step="RESUME")
        else:
            run = self._resume(job, ws, repo, session, prompt, item_ids=item_ids, step="RESUME")
        w = self._finish_writer(job, ws, repo, run, previous_head=packet.head_sha, item_ids=item_ids, step="RESUME")
        dispositions: dict[int, st.Disposition] = {}
        for d in w.result.dispositions:
            if not d.item_id.isdigit() or int(d.item_id) not in {i.id for i in items}:
                continue
            if d.disposition == "fixed":
                dispositions[int(d.item_id)] = st.Disposition("fixed", fixed_sha=w.gate.cited.get(d.commit_sha or ""))
            else:
                dispositions[int(d.item_id)] = st.Disposition("declined", reason=d.reason or "(no reason)")
        result_doc = {"status": w.result.status, "summary": w.result.summary,
                      "dispositions": [asdict(d) for d in w.result.dispositions], "head": w.gate.head}
        packet, _ = store.record_resume_result(packet.id, result=result_doc, dispositions=dispositions)
        return self._after_resume(job, repo, packet)

    def _after_resume(self, job: st.Job, repo: RepoConfig, packet: st.Packet) -> st.Job:
        job = self.store.get_job(job.id)
        ws = self._ws_of(job)
        if packet.kind == "round" and packet.round_id is not None:
            self._post_round_comment(job, repo, self.store.get_round(packet.round_id), packet)
        head = self._local_head(ws)
        if head == packet.head_sha and packet.kind == "round":
            # R8: every item declined, so the round closes on the same head. A fresh round
            # on it (seq+1) re-reads checks and catches late items, then READY is evaluated.
            return self.store.set_state(job.id, "ROUND")
        return self.store.set_state(job.id, "VERIFY")

    def _post_round_comment(self, job: st.Job, repo: RepoConfig, rnd: st.Round, packet: st.Packet | None) -> None:
        """D2, R9: one summary comment per completed round, with the gaffer-round marker;
        skipped when gaffer's own author already posted that marker."""
        if job.pr_number is None:
            return
        outcome = self._round_outcome(job, rnd)
        lines = [f"gaffer: round {rnd.seq} on `{_short(rnd.head_sha)}` "
                 f"(repair cycle {job.cycles_used} of {job.cycle_limit})", ""]
        cr = outcome.get("coderabbit")
        lines.append("- CodeRabbit: " + ("verified (Review completed)" if cr == "complete"
                                         else f"**unverified**: {outcome.get('coderabbit_reason') or cr}"))
        checks = outcome.get("checks") or []
        if outcome.get("no_ci_evidence"):
            lines.append("- Required checks: NO CI EVIDENCE (allow_no_ci)")
        else:
            lines.append("- Required checks: " + ", ".join(f"{c['name']} {c['state']}" for c in checks))
        review = self.store.review_for_head(job.id, rnd.head_sha)
        if review is not None:
            lines.append(f"- Codex review: {len(review.result.get('findings', []))} finding(s)")
        if packet is None:
            lines.append("- Feedback: nothing to address in this round.")
        else:
            lines += ["", "Dispositions:"]
            for i in self.store.packet_items(packet.id):
                what = (f"fixed in {_short(i.fixed_sha)}" if i.disposition == "fixed"
                        else f"declined: {i.decline_reason}" if i.disposition == "declined" else "no disposition")
                lines.append(f"- {_prompt_source(i)} {_prompt_location(i) or i.item_id}: {what}")
        summary = "\n".join(lines)
        gh = self._github(repo)
        res = ws_mod.post_round_summary(gh.client, job.pr_number, job_id=job_marker(job), head=rnd.head_sha,
                                        seq=rnd.seq, summary=summary, operator_login=self.cfg.operator_login,
                                        scanner=self.d.scanner)
        self.store.log_event(job.id, "round_comment", {"round_id": rnd.id, "posted": res.posted, "url": res.url,
                                                       "marker": ws_mod.round_marker(job_marker(job), rnd.head_sha, rnd.seq)})

    # -- READY --------------------------------------------------------------------

    def _evaluate_ready(self, job: st.Job, repo: RepoConfig, rnd: st.Round) -> st.Job:
        """Design "Exits": READY only on the current remote head with every condition met;
        otherwise BLOCKED: not_ready listing what is unmet (R8, SC7)."""
        store = self.store
        job = store.get_job(job.id)
        ws = self._ws_of(job)
        head = job.head_sha
        unmet: list[str] = []
        outcome = self._round_outcome(job, rnd)
        if rnd.head_sha != head:
            unmet.append(f"the last complete round is on {_short(rnd.head_sha)}, not the head {_short(head)}")
        if not (outcome.get("checks_all_passed") or outcome.get("no_ci_evidence")):
            bad = [f"{c['name']} {c['state']}" for c in outcome.get("checks", []) if c["state"] != "pass"]
            unmet.append("required checks not all passed: " + ", ".join(bad))
        if outcome.get("coderabbit") == "pending":
            unmet.append("CodeRabbit hasn't finished on the head")
        if store.review_for_head(job.id, head) is None:
            unmet.append("no Codex review recorded for the head")
        if not self._verified(job, head):
            unmet.append("gaffer's own test run hasn't passed on the head")
        pending = [i for i in store.pending_items(job.id) if i.source != "check" and i.source != "test"]
        if pending:
            unmet.append(f"{len(pending)} admitted item(s) were never packaged")
        undisposed = [i for i in store.items(job.id) if i.admitted and i.processed_at is not None and i.disposition is None]
        if undisposed:
            unmet.append(f"{len(undisposed)} item(s) have no disposition")
        try:
            remote = ws_mod.remote_head(ws, token=self._git_token())
        except ws_mod.GitError as e:
            raise RuntimeError(f"could not read the remote head: {e}") from None
        if remote != head:
            unmet.append(f"the remote head is {_short(remote) or 'missing'}, not {_short(head)}")
        if unmet:
            raise Blocked("not_ready", "; ".join(unmet), step="ROUND")
        return store.set_state(job.id, "READY")

    # -- --adopt-head (R8) ----------------------------------------------------------

    def _adopt_head(self, job: st.Job, repo: RepoConfig) -> st.Job:
        ws = self._ws_of(job)
        url = ws.remote_url
        env, secrets = self._git_auth(url)
        remote = ws_mod.remote_head(ws, token=self._git_token())
        if remote is None:
            raise Refused(f"{ws.branch} doesn't exist on the remote; nothing to adopt.")
        ws_mod.git(ws.clone, "fetch", "--quiet", "--no-tags", url,
                   f"+refs/heads/{ws.branch}:refs/remotes/origin/{ws.branch}",
                   f"+refs/heads/{repo.base}:refs/remotes/origin/{repo.base}", env=env, secrets=secrets)
        ws_mod.git(ws.clone, "checkout", "--quiet", "-f", "-B", ws.branch, remote)
        tester = job.tester_commit_sha
        if tester and not ws_mod._is_ancestor(ws.clone, tester, remote):
            raise Blocked("tester_commit_rewritten", f"the remote head {remote} doesn't contain the tester commit {tester}")
        base = ws_mod._out(ws.clone, "merge-base", f"refs/remotes/origin/{repo.base}", remote)
        job = self.store.adopt_head(job.id, head_sha=remote, base_sha=base)
        ws.base_sha = base
        self._save_ws(job, ws)
        self._say(f"gaffer: adopted {remote} as gaffer's head; base is now the merge-base {base} (R8).")
        return self.store.set_state(job.id, "VERIFY")

    # -- report -------------------------------------------------------------------

    def report_path(self, job: st.Job) -> Path:
        return self._state_dir(job) / "report.md"

    def _print_report(self, job: st.Job, *, regenerate: bool = False) -> None:
        path = self.report_path(job)
        if regenerate or not path.exists():
            text = build_report(self.store, self.cfg, job)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = path.with_name("report.md.tmp")
            tmp.write_text(text)
            os.replace(tmp, path)
        self._say(path.read_text().rstrip())
        self._say(f"\n(report: {path})")

    def _say(self, text: str) -> None:
        print(text, file=self.d.out, flush=True)


# --- conversions ------------------------------------------------------------------------


def _role_name(role: str) -> str:
    return {"test_run": "test run"}.get(role, role)


def _limit_text(limit: ch.LimitEvent) -> str:
    reset = f"; resets at {_now_iso(limit.resets_at)}" if limit.resets_at else ""
    return f"{limit.provider} usage limit: {limit.detail}{reset}"


def _outcome(result: Any) -> str:
    if isinstance(result, ch.WriterRun):
        return "ok" if result.result else (f"limit" if result.limit else "no_result")
    if isinstance(result, (ch.TesterRun, ch.ReviewRun)):
        return "ok" if result.result else ("limit" if result.run.limit else "no_result")
    if isinstance(result, ch.CommandRun):
        return "timeout" if result.timed_out else f"exit {result.exit_code}"
    return "ok"


def _usage(result: Any) -> Any:
    if isinstance(result, ch.WriterRun):
        return {"usage": dict(result.usage), "num_turns": result.num_turns, "duration": result.duration,
                "model": result.model, "claude_version": result.claude_version, "skills": list(result.skills)}
    if isinstance(result, (ch.TesterRun, ch.ReviewRun)):
        r = result.run
        return {"usage": dict(r.usage), "rate_limits": dict(r.rate_limits) if r.rate_limits else None,
                "duration": r.duration, "model": r.model}
    if isinstance(result, ch.CommandRun):
        return {"duration": result.duration, "exit_code": result.exit_code}
    return None


def _criterion_doc(tr: ch.TesterResult, write: ws_mod.TesterWrite) -> dict[str, Any]:
    return {
        "criteria": [list(c) for c in tr.criteria],
        "untestable": [list(u) for u in tr.untestable],
        "tests": [{"criterion_id": t.criterion_id, "label": t.label, "file": t.identity.file,
                   "classname": t.identity.classname, "name": t.identity.name} for t in tr.tests],
        "expected_missing": list(tr.expected_missing),
        "created": list(write.created),
        "modified": list(write.modified),
    }


def _tester_result(doc: Mapping[str, Any]) -> ch.TesterResult:
    return ch.TesterResult(
        criteria=tuple((c[0], c[1]) for c in doc["criteria"]),
        untestable=tuple((u[0], u[1]) for u in doc["untestable"]),
        tests=tuple(ch.TesterTest(t["criterion_id"], t["label"], ch.TestIdentity(t["file"], t["classname"], t["name"]))
                    for t in doc["tests"]),
        expected_missing=tuple(doc["expected_missing"]),
        files=(),
    )


def _criterion_map(doc: Mapping[str, Any]) -> rv_junit.CriterionMap:
    """ADAPTER: built here rather than with CriterionMap.from_tester_output, which
    reads `untestable[].id` while children's tester schema says `criterion_id`."""
    untestable = {u[0] for u in doc["untestable"]}
    return rv_junit.CriterionMap(
        tests=tuple(rv_junit.ExpectedTest(t["criterion_id"], rv_junit.Label(t["label"]), t["file"], t["classname"], t["name"])
                    for t in doc["tests"]),
        expected_missing=tuple(doc["expected_missing"]),
        testable_criteria=tuple(c[0] for c in doc["criteria"] if c[0] not in untestable),
    )


def _case_results(run: rv_junit.RunResult, cmap: rv_junit.CriterionMap,
                  inv: rv_junit.Inventory | None = None) -> list[st.CaseResult]:
    out: dict[tuple[str, str, str], st.CaseResult] = {}
    for c in run.cases:
        key = (c.file, c.classname, c.name)
        out[key] = st.CaseResult(c.file, c.classname, c.name, c.outcome.value, c.exc_type or None, (c.summary or None) and c.summary[:2000])
    if inv is not None:
        for e in inv.entries:
            key = (e.test.file, e.test.classname, e.test.name)
            if e.outcome is rv_junit.Outcome.MISSING and key not in out:
                out[key] = st.CaseResult(*key, "missing", None, e.detail[:2000] or None)
    return list(out.values())


def _item_doc(it: rv_cr.FeedbackItem) -> dict[str, Any]:
    return {"source": str(it.key[0]), "item_id": it.item_id, "content_hash": it.content_hash, "kind": str(it.kind),
            "author": it.author, "body": it.body, "path": it.path, "line_start": it.line_start, "line_end": it.line_end,
            "url": it.url, "context": list(it.context)}


def _feedback_item(i: st.FeedbackItem) -> rv_cr.FeedbackItem:
    p = i.payload or {}
    return rv_cr.FeedbackItem(source=rv_cr.Source(i.source), item_id=i.item_id, content_hash=i.content_hash,
                              kind=p.get("kind", ""), author=p.get("author", ""), body=p.get("body", ""),
                              path=p.get("path"), line_start=p.get("line_start"), line_end=p.get("line_end"),
                              url=p.get("url", ""), context=tuple(p.get("context") or ()))


def _check_run_doc(c: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("id", "name", "status", "conclusion", "html_url", "output", "app", "started_at")
    return {k: c.get(k) for k in keep if k in c}


def _round_doc(state: rv_rounds.RoundState) -> dict[str, Any]:
    d = state.to_dict()
    d.pop("supervised_seconds", None)  # the store's rounds row owns supervised time (R25)
    return d


def _cr_state(state: rv_rounds.RoundState) -> str | None:
    if state.coderabbit_state == "complete":
        return "verified"
    if state.coderabbit_state == "unverified":
        return "unverified" if state.coderabbit_reason else None
    return None


def _prompt_source(i: st.FeedbackItem) -> str:
    return {"check": "ci", "test": "gaffer-verify"}.get(i.source, i.source)


def _prompt_location(i: st.FeedbackItem) -> str:
    p = i.payload or {}
    if p.get("path"):
        return f"{p['path']}:{p['line_start']}" if p.get("line_start") else str(p["path"])
    if i.source == "check":
        return str(p.get("name") or "")
    if i.source == "test":
        return str(p.get("test") or "")
    return ""


def _prompt_body(i: st.FeedbackItem) -> str:
    p = i.payload or {}
    if i.source in ("check", "test"):
        return str(p.get("text") or "")
    body = str(p.get("body") or "")
    for reply in p.get("context") or ():
        body += f"\n\n> thread reply (context):\n{reply}"
    return body


# --- report --------------------------------------------------------------------------------

SKIPPED_GATES = ("staging deployment", "legal review", "live calls to external services", "paid evaluations")


def build_report(store: st.Store, cfg: Config, job: st.Job) -> str:
    """report.md, built from the store only (no GitHub or Linear call), so a READY
    rerun reprints it offline (R8)."""
    repo = cfg.repos.get(job.team_key)
    ev = lambda kind: store.events(job.id, kind)  # noqa: E731
    lines: list[str] = []
    marker = job_marker(job)
    lines.append(f"# gaffer report: {marker} — {job.state}")
    lines.append("")
    if job.state == "BLOCKED":
        lines += [f"**BLOCKED: {job.blocked_reason}** (rerun re-validates from {RERUN_STEP.get(job.blocked_reason or '', job.blocked_step) or 'the start'})",
                  "", f"{job.blocked_message}", "", f"What to do: {BLOCKED_HELP.get(job.blocked_reason or '', 'see the message above.')}", ""]
        for d in ev("blocked_detail"):
            if d.detail.get("reason") == job.blocked_reason and (d.detail.get("detail") or {}).get("question"):
                lines += [f"Writer's question: {d.detail['detail']['question']}", ""]
    if job.state == "CLOSED":
        why = ev("closed")
        lines += [f"**CLOSED**: {why[-1].detail['why'] if why else 'the PR was closed or merged by someone else'}.", ""]
    no_ci = repo is not None and not repo.required_checks
    if job.state == "READY" and no_ci:
        lines += ["**NO CI EVIDENCE**: this repo has no required checks (allow_no_ci = true, R11).", ""]

    issue = ev("intake")
    pr = ev("pr")
    lines.append("## Job")
    lines.append(f"- Issue: {job.issue_key}" + (f" — {issue[-1].detail.get('title')}" if issue else ""))
    lines.append(f"- Branch: `{job.branch}`; PR: {pr[-1].detail['url'] if pr else (f'#{job.pr_number}' if job.pr_number else 'none')}")
    lines.append(f"- Remote head (verified at READY): `{job.head_sha or 'not pushed'}`" if job.state == "READY"
                 else f"- Last pushed head: `{job.head_sha or 'not pushed'}`")
    lines.append(f"- Base: `{job.base_sha or '-'}`; tester commit: `{job.tester_commit_sha or '-'}`")
    lines.append(f"- Repair cycles: {job.cycles_used} of {job.cycle_limit}"
                 + (f" (granted with --more-cycles: {', '.join(str(n) for n, _, _ in store.cycle_grants(job.id))})"
                    if store.cycle_grants(job.id) else ""))
    rounds_all = store.rounds(job.id)
    done = [r for r in rounds_all if r.outcome == "complete" and r.head_sha == job.head_sha]
    if done:
        r = done[-1]
        lines.append("- CodeRabbit on the head: " + ("verified (Review completed)" if r.coderabbit == "verified"
                                                      else f"**unverified**: {r.coderabbit_note}"))
    if job.reconstructed:
        lines.append("- **Reconstructed** after state_lost: base and tester commit are inferred (R9, R23).")
    for a in ev("adopt_head"):
        lines.append(f"- --adopt-head: accepted `{a.detail['head_sha']}`, base now `{a.detail['base_sha']}` (R8).")
    lines.append("")

    # R30: CI and harness changes, prominently.
    guarded = [c for e in ev("guarded_changes") for c in e.detail["changes"]]
    seen: set[tuple[str, str]] = set()
    guarded = [c for c in guarded if not ((c["commit"], c["path"]) in seen or seen.add((c["commit"], c["path"])))]
    ci = [c for c in guarded if c["kind"] == "ci"]
    if ci:
        lines.append("## ⚠ CI workflow changes (R30)")
        lines.append("These run with the repo's Actions secrets once CI runs on the PR.")
        lines += [f"- `{c['path']}` in `{_short(c['commit'])}`: {c.get('reason') or '(no reason)'}" for c in ci]
        lines.append("")
    if repo is not None:
        gh_hosts = [h for h in repo.network_allowlist if h.endswith("github.com")]
        if gh_hosts:
            lines.append("## GitHub hosts open to sandboxed code (PHP, R29)")
            lines.append(f"- {', '.join(gh_hosts)} were reachable from the writer's Bash and install_command; "
                         f"github.com stayed blocked; gitleaks scanned every push and comment.")
            lines.append("")

    # gaffer's own test run and the criterion map.
    lines.append("## gaffer's test run")
    verify = [e for e in ev("verify") if e.detail.get("head") == job.head_sha] or ev("verify")
    cmap = job.criterion_map or {}
    if verify:
        v = verify[-1].detail
        lines.append(f"- Head `{_short(v['head'])}`: {'passed' if v['passed'] else 'FAILED'}")
        for note in v.get("notes", []):
            lines.append(f"- Note: {note}")
        texts = {c[0]: c[1] for c in cmap.get("criteria", [])}
        lines.append("")
        lines.append("| Criterion | Test | Label | Result |")
        lines.append("|---|---|---|---|")
        for e in v.get("inventory", []):
            lines.append(f"| {e['criterion']}: {texts.get(e['criterion'], '')[:80]} | `{e['test']}` | {e['label']} | {e['outcome']} |")
        for r in v.get("regressions", []):
            lines.append(f"| (suite) | `{r}` | regression | failed |")
    else:
        lines.append("- No verification run yet.")
    lines.append("")
    if cmap.get("untestable"):
        lines.append("## Unverified criteria (untestable)")
        lines += [f"- {u[0]}: {u[1]}" for u in cmap["untestable"]]
        lines.append("")
    if cmap.get("created") or cmap.get("modified"):
        lines.append("## Tester files (R6)")
        lines += [f"- created `{p}`" for p in cmap.get("created", [])]
        lines += [f"- modified existing `{p}`" for p in cmap.get("modified", [])]
        lines.append("")
    other = [c for c in guarded if c["kind"] != "ci"]
    if other:
        lines.append("## Writer changes to tester and harness files (R7)")
        lines += [f"- {c['kind']} `{c['path']}` in `{_short(c['commit'])}`: {c.get('reason') or '(no reason)'}" for c in other]
        lines.append("")

    # Rounds, CodeRabbit verified/unverified (R27), supervised vs wall time (R25).
    rounds = store.rounds(job.id)
    if rounds:
        lines.append("## Rounds")
        for r in rounds:
            cr = ("CodeRabbit verified" if r.coderabbit == "verified" else
                  f"CodeRabbit **unverified**: {r.coderabbit_note}" if r.coderabbit == "unverified" else "CodeRabbit pending")
            wall = r.wall_elapsed(r.closed_at or r.last_tick_at or r.opened_at)
            lines.append(f"- Round {r.seq} on `{_short(r.head_sha)}`: {r.outcome or 'open'}; {cr}; "
                         f"supervised {r.supervised_seconds:.0f} s, wall {wall:.0f} s")
        for c in ev("round_comment"):
            lines.append(f"- Round summary comment `{c.detail['marker']}`: {'posted' if c.detail['posted'] else 'already there'}")
        gaps = ev("sleep_gap")
        for g in gaps:
            lines.append(f"- Sleep gap: {g.detail['gap_seconds']:.0f} s not counted (R17, R25)")
        lines.append("")

    # Codex review (and harness judgements, R7).
    if job.head_sha and (rev := store.review_for_head(job.id, job.head_sha)) is not None:
        lines.append("## Codex review of the head")
        lines.append(f"- {rev.result.get('summary', '')}")
        for j in rev.result.get("harness_judgements", []):
            lines.append(f"- Harness change `{j['path']}`: {j['verdict']} — {j['reason']}")
        lines.append("")

    # Dispositions of every feedback item, and ignored ones (R12).
    items = [i for i in store.items(job.id) if i.admitted]
    if items:
        lines.append("## Feedback items")
        for i in items:
            p = i.payload or {}
            where = _prompt_location(i)
            what = (f"fixed in `{_short(i.fixed_sha)}`" if i.disposition == "fixed"
                    else f"declined: {i.decline_reason}" if i.disposition == "declined"
                    else "in a packet, awaiting the writer" if i.packet_id and not i.processed_at
                    else "processed without a disposition" if i.processed_at else "not yet in a packet")
            lines.append(f"- [{_prompt_source(i)}] {where or i.item_id}: {what}")
        lines.append("")
    ignored = [d for e in ev("round_outcome") for d in e.detail.get("ignored", [])]
    if ignored:
        lines.append("## Ignored comments (not feedback, R12)")
        uniq = {(d["source"], d["item_id"]): d for d in ignored}
        lines += [f"- {d['source']} {d['kind']} {d['item_id']} by {d['author']}: {d['reason']}" for d in uniq.values()]
        lines.append("")
    not_reviewed = sorted({f for e in ev("round_outcome") for f in e.detail.get("not_reviewed_files", [])})
    if not_reviewed:
        lines.append("## Files CodeRabbit didn't review")
        lines += [f"- {f}" for f in not_reviewed]
        lines.append("")

    # Isolation and evidence.
    lines.append("## Isolation and environment")
    evid = store.cli_evidence(job.id)
    if evid:
        e = evid[-1]
        lines.append(f"- CLIs: claude {e.claude_version}, codex {e.codex_version}; "
                     f"gitleaks {(e.extra or {}).get('gitleaks')}; sandbox-runtime {(e.extra or {}).get('srt') or 'n/a'}")
        mcp = (e.extra or {}).get("mcp_servers") or []
        lines.append(f"- MCP servers outside the sandbox (R3): {', '.join(mcp) if mcp else 'none'}")
    if repo is not None:
        lines.append(f"- Writer model: {cfg.writer_model} (R30); network allowlist: {', '.join(repo.network_allowlist) or 'none'}")
    skills = sorted({s for e in ev("writer_run") for s in e.detail.get("skills", [])})
    lines.append(f"- Synced skills the writer saw (R28): {', '.join(skills) if skills else 'none'}")
    blocked_plugins = [p for e in ev("plugins_blocked") for p in e.detail.get("plugins", [])]
    if blocked_plugins:
        lines.append(f"- Newly synced plugins blocked and the writer relaunched (R31): {', '.join(blocked_plugins)}")
    restores = ev("git_config_restored")
    if restores:
        lines.append(f"- Job clone `.git/config` restored {len(restores)} time(s) (R31):")
        lines += [f"  - {r.detail['note']}" for r in restores]
    else:
        lines.append("- Job clone `.git/config`: never changed outside gaffer (R31)")
    for f in ev("fresh_writer"):
        lines.append(f"- Fallback: fresh writer session `{f.detail['new_session']}` with a handoff ({f.detail['why']})")
    for s in ev("writer_group_sigterm"):
        lines.append(f"- Writer process group {s.detail.get('pgid')} outlived the 2 min grace and was SIGTERMed (R9)")
    lines.append("")

    # Usage per attempt (design "Seed data for B").
    attempts = store.attempts(job.id)
    if attempts:
        lines.append("## Attempts and usage")
        for a in attempts:
            u = a.usage or {}
            bits = []
            if isinstance(u, dict):
                if u.get("usage"):
                    bits.append(", ".join(f"{k} {v}" for k, v in u["usage"].items() if isinstance(v, (int, float))))
                if u.get("duration") is not None:
                    bits.append(f"{u['duration']:.0f} s")
            lines.append(f"- {a.step} {a.role}: {a.outcome or 'running'}" + (f" ({'; '.join(b for b in bits if b)})" if bits else ""))
        lines.append("")

    lines.append("## Not covered by gaffer")
    lines += [f"- {g}: not covered" for g in SKIPPED_GATES]
    lines.append("")
    return "\n".join(lines)


# --- CLI -----------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse's exit 2 would read as BLOCKED (R8)
        self.print_usage(sys.stderr)
        print(f"gaffer: {message} (refused)", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="gaffer", description="Overnight pair: a Codex tester and a Claude writer, supervised by gaffer.")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run (or continue) the job for one Linear issue")
    run.add_argument("issue", help="Linear issue key, e.g. ENG-123")
    run.add_argument("--adopt-head", action="store_true", help="accept the PR's current head after foreign_head or base_conflict (R8)")
    run.add_argument("--more-cycles", type=int, default=0, metavar="N", help="grant N more repair cycles, recorded (R8)")
    run.add_argument("--continue", dest="cont", action="store_true", help="process feedback that arrived after READY (R8)")
    run.add_argument("--fresh", action="store_true", help="after CLOSED: archive the job and start gaffer/<key>-2 (R8)")
    return p


def main(argv: Sequence[str] | None = None, *, home: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.more_cycles < 0:
        print("gaffer: --more-cycles needs a positive N (refused)", file=sys.stderr)
        return EXIT_REFUSED
    key = args.issue
    if not ISSUE_KEY.fullmatch(key):
        print(f"gaffer: {key!r} is not a Linear issue key like ENG-123 (refused).", file=sys.stderr)
        return EXIT_REFUSED
    home = home or Path.home()
    try:
        cfg = gconfig.load_config(home=home)
        store = st.Store(st.default_db_path(home))
    except (gconfig.ConfigError, st.StoreError) as e:
        print(f"gaffer: {e}", file=sys.stderr)
        return getattr(e, "exit_code", EXIT_ERROR)

    token_cache: dict[str, Secret] = {}

    def token() -> Secret:
        if "gh" not in token_cache:
            token_cache["gh"] = gconfig.github_token()
        return token_cache["gh"]

    def github(repo: RepoConfig) -> GitHubPorts:
        t = token()
        return GitHubPorts(ws_mod.GitHubClient(repo.github, t), rv_github.UrllibGitHubReader(t))

    deps = Deps(cfg=cfg, store=store, children=RealChildren(cfg), github=github,
                linear=lambda: LinearClient(linear_token()), scanner=ws_mod.Gitleaks(), clock=st.SystemClock(),
                sleep=time.sleep, home=home, token=token)
    try:
        return Runner(deps).run(key, Options(adopt_head=args.adopt_head, more_cycles=args.more_cycles,
                                             cont=args.cont, fresh=args.fresh))
    finally:
        store.close()
