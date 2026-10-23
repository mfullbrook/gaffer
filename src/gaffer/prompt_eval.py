"""`gaffer eval`: the three-ticket prompt eval with stored baselines (R19, decision A).

`evals/` holds a toy Python repo (`evals/toy/`, plain files, no nested .git) and three seeded
tickets (`evals/tickets/EVAL-<n>/ticket.toml`, standing in for Linear issues): a bug fix, a new
feature with a missing symbol, and a dependency add installed from PyPI through the allowlisted
install path (R14). Each ticket also carries one seeded feedback item, its expected outcomes and
a hidden test file that no child ever sees.

Each run makes a fresh git repo of the toy plus a local bare repo standing in for GitHub, then
drives every ticket through the real runner (`EvalRunner`, a `runner.Runner`): PREFLIGHT and the
R18 doctor gate, INTAKE (the ticket file), WORKSPACE, TESTER, RED_FIRST, WRITER, VERIFY with its
local repair loop (R24), then, in place of PUBLISH and ROUND, a Codex review of the verified head
(review.md) and the seeded feedback packet resumed through address.md, then VERIFY again. The
children, workspace, reviews code and prompts are the production ones. There is no push, PR,
CodeRabbit, Linear call or GitHub token: the GitHub port raises if anything reaches it.

After the run gaffer copies the ticket's hidden tests into a verification worktree of the final
head and runs them with the toy's tests (sandboxed, no network, no model), so the outcome is
judged independently of the tester's own tests.

Storage (JSON, not state.db): `~/.gaffer/evals/runs/<run>.json` per run and
`~/.gaffer/evals/baseline.json`. Each run's scratch (one state.db per ticket, logs, report.md)
is under `~/.gaffer/evals/runs/<run>/`, denied to children like the rest of ~/.gaffer (R28); the
job clones are under a temp job root. The first complete run becomes the baseline; later runs
are compared with it (regressions exit 2) until `--save-baseline` or `--promote RUN` replaces
it. Run manually before merging a change under prompts/, never in CI (R19).
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from gaffer import children as ch
from gaffer import config as gconfig
from gaffer import runner as rn
from gaffer import store as st
from gaffer import workspace as ws_mod
from gaffer.config import Config
from gaffer.reviews import junit as rv_junit
from gaffer.reviews import packet as rv_packet
from gaffer.reviews import redfirst as rv_redfirst

EVALS_DIR = Path(__file__).resolve().parents[2] / "evals"
TEAM = "EVAL"
RECORD_VERSION = 1

# Exit codes, following R8's: 0 baseline stored or no regression, 1 internal error, 2 regression,
# 3 incomplete (a usage limit or error stopped the run; nothing was promoted), 4 refused (lock,
# config, preflight or doctor).
EXIT_PASS = 0
EXIT_ERROR = rn.EXIT_ERROR
EXIT_REGRESSION = 2
EXIT_INCOMPLETE = 3
EXIT_REFUSED = rn.EXIT_REFUSED

FEEDBACK_SOURCES = ("coderabbit", "operator", "codex")
RED_FIRST_KINDS: dict[str, frozenset[rv_redfirst.Verdict]] = {
    "assertion": frozenset({rv_redfirst.Verdict.VALID_ASSERTION}),
    "missing_symbol": frozenset({rv_redfirst.Verdict.VALID_MISSING_SYMBOL, rv_redfirst.Verdict.VALID_COLLECTION_ERROR}),
}
HIDDEN_TEST_PATH = "tests/test_eval_hidden.py"
_HIDDEN_MODULE = "tests.test_eval_hidden"
_HIDDEN_TEST_DEF = re.compile(r"^def (test_\w+)\(", re.MULTILINE)
_RUN_ID = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}")

# The toy repo's settings (R4, R22, R28: pytest keeps going past collection errors). The install
# script keeps uv's cache under TMPDIR, inside the job dir, so it works under sandbox-runtime.
TOY_TEST_COMMAND = ".venv/bin/python -m pytest -q -p no:cacheprovider --continue-on-collection-errors --junitxml=junit.xml"
TOY_INSTALL_COMMAND = "sh scripts/install.sh"
TOY_OPERATOR_LOGIN = "gaffer-eval"  # never used: eval posts nothing
TOY_GITHUB = "gaffer-eval/toy"  # never contacted: origin is a local bare repo

# Stage outcomes, best first. A success turning into a failure is a regression; a first-try
# success turning into a success after a retry or repair is only a warning (models are noisy).
STAGES = ("tester", "red_first", "writer", "verify", "review", "address", "verify_after_address", "hidden_tests")
SUCCESS = frozenset({"ok", "valid", "passed", "fixed", "declined"})
DEGRADED = frozenset({"ok_after_retry", "valid_after_retry", "passed_after_repair"})
# Growth past both of these is reported as "runs longer" (a warning, never a regression).
SLOWER_RATIO = 1.5
SLOWER_SECONDS = 120.0
MORE_TURNS = 5


class EvalError(Exception):
    """A broken evals/ fixture or stored record: exit 1."""


class NoNetwork(Exception):
    """gaffer eval reached for GitHub or Linear: never allowed (R19: no push, PR or CodeRabbit)."""


# --- tickets ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Feedback:
    """The one seeded feedback item resumed through address.md."""

    source: str
    body: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class Expected:
    red_first: str  # how every acceptance test is red on the base: "assertion" or "missing_symbol"
    address: tuple[str, ...]  # dispositions that answer the seeded item correctly
    dependency: str | None = None  # a package the final head's requirements.txt must declare


@dataclass(frozen=True)
class Ticket:
    key: str
    kind: str
    title: str
    description: str
    feedback: Feedback
    expected: Expected
    hidden_test: str  # never shown to a child

    def issue(self) -> rn.Issue:
        return rn.Issue(self.key, self.title, self.description, url="")

    @property
    def hidden_tests(self) -> tuple[str, ...]:
        return tuple(_HIDDEN_TEST_DEF.findall(self.hidden_test))


def load_tickets(root: Path) -> list[Ticket]:
    """Every `<root>/<KEY>/ticket.toml` with its `hidden_test.py`, in key order."""
    tickets: list[Ticket] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        tickets.append(_load_ticket(d))
    if not tickets:
        raise EvalError(f"no tickets under {root}")
    return sorted(tickets, key=lambda t: int(t.key.rsplit("-", 1)[1]))


def _load_ticket(d: Path) -> Ticket:
    where = d / "ticket.toml"
    try:
        data = tomllib.loads(where.read_text())
        hidden = (d / "hidden_test.py").read_text()
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise EvalError(f"{where}: {e}") from None

    def need(table: Mapping[str, Any], key: str, kind: type, what: str) -> Any:
        value = table.get(key)
        if not isinstance(value, kind) or (isinstance(value, str) and not value.strip()):
            raise EvalError(f"{where}: {what} must be a non-empty {kind.__name__}")
        return value

    key = need(data, "key", str, "key")
    if not rn.ISSUE_KEY.fullmatch(key) or key.rsplit("-", 1)[0] != TEAM or key != d.name:
        raise EvalError(f"{where}: key {key!r} must be {TEAM}-<n> and match its directory name")
    fb = need(data, "feedback", dict, "[feedback]")
    source = need(fb, "source", str, "feedback.source")
    if source not in FEEDBACK_SOURCES:
        raise EvalError(f"{where}: feedback.source must be one of {', '.join(FEEDBACK_SOURCES)}")
    line = fb.get("line")
    if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
        raise EvalError(f"{where}: feedback.line must be a positive line number")
    exp = need(data, "expected", dict, "[expected]")
    red = need(exp, "red_first", str, "expected.red_first")
    if red not in RED_FIRST_KINDS:
        raise EvalError(f"{where}: expected.red_first must be one of {', '.join(RED_FIRST_KINDS)}")
    address = tuple(need(exp, "address", list, "expected.address"))
    if not address or any(a not in st.DISPOSITIONS for a in address):
        raise EvalError(f"{where}: expected.address must list dispositions from {', '.join(st.DISPOSITIONS)}")
    dependency = exp.get("dependency")
    if dependency is not None and (not isinstance(dependency, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dependency)):
        raise EvalError(f"{where}: expected.dependency must be a package name")
    ticket = Ticket(key=key, kind=need(data, "kind", str, "kind"), title=need(data, "title", str, "title"),
                    description=need(data, "description", str, "description").strip() + "\n",
                    feedback=Feedback(source, need(fb, "body", str, "feedback.body").strip(), fb.get("path"), line),
                    expected=Expected(red, address, dependency), hidden_test=hidden)
    if rn.acceptance_criteria(ticket.description) is None:
        raise EvalError(f"{where}: the description needs an \"Acceptance criteria\" section (design INTAKE)")
    if not ticket.hidden_tests:
        raise EvalError(f"{d / 'hidden_test.py'}: no test functions")
    return ticket


class TicketLinear:
    """runner.LinearPort over the ticket files: INTAKE reads them instead of Linear."""

    def __init__(self, tickets: Sequence[Ticket]):
        self._tickets = {t.key: t for t in tickets}
        self.calls: list[str] = []

    def fetch_issue(self, key: str) -> rn.Issue:
        self.calls.append(key)
        return self._tickets[key].issue()


def no_github(repo: gconfig.RepoConfig) -> rn.GitHubPorts:
    raise NoNetwork(f"gaffer eval never calls GitHub (asked for {repo.github}); this is a bug in the eval seam")


# --- the toy repo -------------------------------------------------------------------------


def make_toy(src: Path, dest: Path) -> tuple[Path, Path]:
    """A fresh git checkout of evals/toy at dest/toy, and a bare clone at dest/toy.git standing in
    for GitHub. The commit's identity and date are fixed, so every run has the same base SHA."""
    checkout, remote = dest / "toy", dest / "toy.git"
    shutil.copytree(src, checkout, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", "junit.xml"))
    env = {"GIT_AUTHOR_NAME": "gaffer eval", "GIT_AUTHOR_EMAIL": "eval@gaffer.invalid",
           "GIT_COMMITTER_NAME": "gaffer eval", "GIT_COMMITTER_EMAIL": "eval@gaffer.invalid",
           "GIT_AUTHOR_DATE": "2026-10-23T00:00:00Z", "GIT_COMMITTER_DATE": "2026-10-23T00:00:00Z"}
    ws_mod.git(checkout, "init", "--quiet", "-b", "main")
    ws_mod.git(checkout, "add", "-A")
    ws_mod.git(checkout, "commit", "--quiet", "-m", "toy: initial", env=env)
    ws_mod.git(dest, "clone", "--quiet", "--bare", str(checkout), str(remote))
    return checkout, remote


def toy_toml(*, checkout: Path, job_root: Path, vault: Path) -> str:
    """gaffer.toml text for the toy repo under team EVAL (validated by config.load_config)."""
    q = json.dumps  # TOML basic strings take JSON's escapes for these values
    return "\n".join([
        f"operator_login = {q(TOY_OPERATOR_LOGIN)}",
        f"vault_path = {q(str(vault))}",
        f"job_root = {q(str(job_root))}",
        "",
        f"[repos.{TEAM}]",
        f"path = {q(str(checkout))}",
        f"github = {q(TOY_GITHUB)}",
        'base = "main"',
        'ecosystems = ["python"]',
        f"test_command = {q(TOY_TEST_COMMAND)}",
        'junit_path = "junit.xml"',
        'runner = "pytest"',
        'test_dirs = ["tests"]',
        'harness_paths = ["scripts/**"]',
        f"install_command = {q(TOY_INSTALL_COMMAND)}",
        "required_checks = []",
        "allow_no_ci = true",
        "",
    ])


def eval_config(user: Config | None, *, home: Path, checkout: Path, job_root: Path, scratch: Path) -> Config:
    """The config every eval child runs under: the toy repo as the only repo, the temp job root,
    and the user's models, vault and allowed MCP when a gaffer.toml is given. Without one the vault
    entry is a stand-in dir, as in `gaffer doctor` (T10)."""
    vault = scratch / "vault-stand-in"
    vault.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = scratch / "eval-gaffer.toml"
    path.write_text(toy_toml(checkout=checkout, job_root=job_root, vault=vault))
    toy = gconfig.load_config(path, home=home)
    if user is None:
        return toy
    return dataclasses.replace(user, job_root=job_root, repos={TEAM: toy.repos[TEAM]})


def prompt_hashes(prompts_dir: Path | None = None) -> dict[str, str]:
    """sha256 of each prompt file the children render (children.PROMPTS_DIR, read at call time)."""
    d = prompts_dir or ch.PROMPTS_DIR
    return {f"{n}.md": hashlib.sha256((d / f"{n}.md").read_bytes()).hexdigest() for n in ch.PROMPT_NAMES}


# --- the runner with a local-only PUBLISH ---------------------------------------------------------


class EvalRunner(rn.Runner):
    """The production state machine with PUBLISH replaced: a Codex review of the verified head and
    the ticket's seeded feedback packet (kind "local", so nothing is posted), resumed through
    address.md; after it, VERIFY again, the hidden tests, then READY. Doctor results live in
    gaffer's real state.db, so eval shares `gaffer run`'s doctor gate."""

    def __init__(self, deps: rn.Deps, ticket: Ticket, *, doctor_store: st.Store, doctor_cfg: Config | None,
                 console: Callable[[str], None] | None = None):
        super().__init__(deps)
        self.ticket = ticket
        self.doctor_store = doctor_store
        self.doctor_cfg = doctor_cfg
        self.console = console or self._say

    def _doctor(self, report: ch.PreflightReport) -> None:
        # The doctor's progress and report go to the console as well as the ticket's log.
        def say(text: str) -> None:
            self._say(text)
            self.console(text)

        rn.doctor_gate(self.doctor_store, self.doctor_cfg, report, self.children.doctor, say)

    def _publish(self, job: st.Job, repo: gconfig.RepoConfig) -> st.Job:
        store = self.store
        ws = self._ws_of(job)
        head = self._local_head(ws)
        if not self._verified(job, head):
            return store.set_state(job.id, "VERIFY")  # R24, as in PUBLISH: only a verified head goes on
        seeded = store.events(job.id, "eval_feedback")
        if seeded:
            if store.get_packet(seeded[-1].detail["packet_id"]).result_recorded_at is None:
                return store.set_state(job.id, "RESUME")
            self._hidden_tests(job, ws, repo, head)
            store.log_event(job.id, "eval_done", {"head": head})
            return store.set_state(job.id, "READY")
        self._codex_review(job, ws, repo, head)
        self._ensure_writer_gone(job)
        self._seed_feedback(job, head)
        return store.set_state(job.id, "RESUME")

    def _seed_feedback(self, job: st.Job, head: str) -> None:
        """The ticket's one feedback item as a packet on the reviewed head, built like a round's
        (gitleaks-scanned, counted as a repair cycle) but local: no round, comment or PR."""
        fb = self.ticket.feedback
        body = fb.body
        item, _ = self.store.add_item(
            job.id, source=fb.source, item_id=f"{head}:eval:{self.ticket.key}", head_sha=head,
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
            payload={"kind": "inline", "author": "gaffer-eval (seeded)", "body": body, "path": fb.path,
                     "line_start": fb.line, "line_end": fb.line, "url": "", "context": []})
        pkt = rv_packet.build_packet(head_sha=head, seq=0, scanner=rn.PacketScanner(self.d.scanner),
                                     items=[rn._feedback_item(item)])
        try:
            packet, _ = self.store.create_packet(job.id, kind="local", head_sha=head, item_ids=[item.id],
                                                 content={"text": pkt.text, "keys": [list(k) for k in pkt.included_keys],
                                                          "eval_feedback": True})
        except st.RepairLimitReached as e:
            raise rn.Blocked("repair_limit", f"no repair cycle left for the seeded feedback on {head[:12]}: {e}",
                             step="PUBLISH") from None
        self.store.log_event(job.id, "eval_feedback", {"packet_id": packet.id, "item_id": item.id, "head": head})

    def _hidden_tests(self, job: st.Job, ws: ws_mod.Workspace, repo: gconfig.RepoConfig, head: str) -> None:
        """The ticket's hidden tests on exactly the final head, in a fresh verification worktree,
        under the same sandboxed test run as VERIFY (R4). A failure to run is recorded, not raised."""
        if self.store.events(job.id, "eval_hidden"):
            return
        names = self.ticket.hidden_tests
        detail: dict[str, Any] = {"head": head, "total": len(names), "passed": 0, "failed": list(names)}
        try:
            install = self._install_runner(job, ws, "VERIFY") if repo.install_command else None
            vc = ws_mod.prepare_verification(ws, repo, head, install=install)
            self._flush_restores(job, ws)
            if vc.install is not None and not vc.install.ok:
                raise RuntimeError(f"install_command exited {vc.install.exit_code}: {vc.install.output_tail[-500:]}")
            (vc.path / HIDDEN_TEST_PATH).parent.mkdir(parents=True, exist_ok=True)
            (vc.path / HIDDEN_TEST_PATH).write_text(self.ticket.hidden_test)
            junit = vc.path / repo.junit_path
            junit.unlink(missing_ok=True)
            self._child(job, "test_run", "VERIFY", None, lambda on_spawn: self.children.run_test_command(
                repo, self._jobs(job, ws), vc.path, command=repo.test_command, on_spawn=on_spawn))
            run = rv_junit.parse_junit(repo.runner, junit.read_bytes() if junit.is_file() else None,
                                       root=str(vc.path), exists=lambda p: (vc.path / p).is_file())
            passed = {c.name for c in run.cases if c.classname == _HIDDEN_MODULE and c.passed}
            detail |= {"passed": sum(n in passed for n in names), "failed": [n for n in names if n not in passed]}
            if not run.usable:
                detail["error"] = "; ".join(run.problems)[:500]
        except (rn.Blocked, ws_mod.WorkspaceError, ch.ChildError, RuntimeError, OSError) as e:
            detail["error"] = f"{type(e).__name__}: {e}"[:500]
        self.store.log_event(job.id, "eval_hidden", detail)


# --- one ticket ----------------------------------------------------------------------------------


@dataclass
class Env:
    """What a run needs; tests swap in fakes (children, scanner, clock) and a temp home."""

    home: Path
    cfg: Config  # eval_config()
    doctor_cfg: Config | None  # what doctor proves: the user's gaffer.toml, or None for defaults
    doctor_store: st.Store  # gaffer's real state.db
    run_dir: Path  # ~/.gaffer/evals/runs/<run>
    remote_url: str
    children: rn.ChildrenPort
    scanner: ws_mod.SecretScanner
    tickets: Sequence[Ticket]
    clock: rn.Clock = field(default_factory=st.SystemClock)
    say: Callable[[str], None] = print  # the console


@dataclass
class TicketRun:
    result: dict[str, Any]
    exit_code: int
    usage_limit: bool = False


def run_ticket(env: Env, ticket: Ticket) -> TicketRun:
    """Drive one ticket from PREFLIGHT to READY or BLOCKED. Refused (lock, preflight, doctor)
    propagates: the whole eval stops."""
    store = st.Store(env.run_dir / f"{ticket.key}.db", clock=env.clock)
    log_path = env.run_dir / f"{ticket.key}.log"
    started = env.clock.wall()
    try:
        with log_path.open("w") as log:
            deps = rn.Deps(cfg=env.cfg, store=store, children=env.children, github=no_github,
                           linear=lambda: TicketLinear(env.tickets), scanner=env.scanner, clock=env.clock,
                           sleep=time.sleep, home=env.home, token=lambda: None, remote_url=lambda repo: env.remote_url,
                           out=log, state_root=env.run_dir / "jobs")
            runner = EvalRunner(deps, ticket, doctor_store=env.doctor_store, doctor_cfg=env.doctor_cfg, console=env.say)
            error = None
            try:
                code = runner._run_locked(ticket.key, rn.Options())
            except (gconfig.ConfigError, gconfig.KeychainError, ch.ChildError, st.StoreError) as e:
                if getattr(e, "exit_code", None) == EXIT_REFUSED:
                    raise rn.Refused(str(e)) from None
                code, error = EXIT_ERROR, f"{type(e).__name__}: {e}"
                print(f"gaffer: error: {error}", file=log)
        job = store.job_for_issue(ticket.key)
        result = collect(store, job, ticket, env.clock.wall() - started)
        result["log"] = str(log_path)
        if error:
            result["error"] = error[:500]
        if runner._preflight is not None:
            result["cli"] = {"claude": runner._preflight.claude_version, "codex": runner._preflight.codex_version}
        return TicketRun(result, code, usage_limit=job is not None and job.blocked_reason == "usage_limit")
    finally:
        store.close()


# --- collecting a ticket's outcome from its store ---------------------------------------------------


def collect(store: st.Store, job: st.Job | None, ticket: Ticket, wall: float) -> dict[str, Any]:
    """The baseline record for one ticket, read back from its state.db."""
    if job is None:
        return {"key": ticket.key, "kind": ticket.kind, "final": "error", "stages": dict.fromkeys(STAGES, "not_reached"),
                "expected": {}, "metrics": {"wall_seconds": round(wall, 1)}}
    ev = lambda kind: store.events(job.id, kind)  # noqa: E731
    attempts = store.attempts(job.id)
    blocked = job.blocked_reason if job.state == "BLOCKED" else None
    seeded = ev("eval_feedback")
    seed_id = seeded[-1].id if seeded else None
    verifies = ev("verify")
    before = [e for e in verifies if seed_id is None or e.id < seed_id]
    after = [e for e in verifies if seed_id is not None and e.id > seed_id]
    final_head = (ev("eval_done") or [None])[-1]
    final_head = final_head.detail["head"] if final_head else (verifies[-1].detail["head"] if verifies else None)
    reds = ev("red_first")
    cmap = job.criterion_map or {}
    tests = cmap.get("tests", [])
    tester_attempts = sum(a.role == "tester" for a in attempts)

    def failed(*steps: str) -> str | None:
        # The BLOCKED reason when the job stopped at one of these steps.
        return blocked if blocked and job.blocked_step in steps else None

    stages: dict[str, str] = {}
    if job.tester_commit_sha or reds:  # the tester produced valid output at least once
        stages["tester"] = "ok" if tester_attempts == 1 else "ok_after_retry"
        if job.tester_commit_sha:
            stages["red_first"] = "valid" if reds and reds[0].detail.get("status") == "ok" else "valid_after_retry"
        else:
            stages["red_first"] = failed("TESTER", "RED_FIRST") or "not_finished"
    else:
        stages["tester"] = failed("TESTER", "RED_FIRST") or "not_reached"
        stages["red_first"] = "not_reached"
    stages["writer"] = "ok" if verifies else (failed("WRITER") or "not_reached")
    stages["verify"] = _verify_stage(before, failed("VERIFY", "RESUME") if not seeded else None)
    review = store.review_for_head(job.id, seeded[-1].detail["head"]) if seeded else None
    stages["review"] = "ok" if review is not None else (failed("PUBLISH") or "not_reached")
    packet = store.get_packet(seeded[-1].detail["packet_id"]) if seeded else None
    item = store.get_item(seeded[-1].detail["item_id"]) if seeded else None
    if packet is not None and packet.result_recorded_at is not None:
        stages["address"] = item.disposition or "no_disposition"
    else:
        stages["address"] = (blocked or "not_reached") if seeded else "not_reached"
    stages["verify_after_address"] = _verify_stage(after, failed("VERIFY", "RESUME") if seeded else None) \
        if packet is not None and packet.result_recorded_at is not None else "not_reached"
    hidden = (ev("eval_hidden") or [None])[-1]
    hd = hidden.detail if hidden else None
    stages["hidden_tests"] = ("not_reached" if hd is None else "error" if hd.get("error") and not hd["passed"]
                              else "passed" if hd["passed"] == hd["total"] else "failed")

    # The acceptance tests' verdicts in the red-first run that was accepted (guards pass on base).
    guard = {rv_redfirst.Verdict.GUARD_PASSES.value, rv_redfirst.Verdict.GUARD_FAILS.value}
    accepted = next((e for e in reversed(reds) if e.detail.get("status") == "ok"), None)
    verdicts = [{"test": v["test"], "verdict": v["verdict"]} for v in (accepted.detail.get("verdicts", []) if accepted else ())
                if v["verdict"] not in guard]
    findings = len((review.result or {}).get("findings", [])) if review else None
    judgements = [j.get("verdict") for j in (review.result or {}).get("harness_judgements", [])] if review else []

    expected: dict[str, bool] = {"completed": job.state == "READY"}
    allowed = RED_FIRST_KINDS[ticket.expected.red_first]
    expected[f"red_first_{ticket.expected.red_first}"] = bool(verdicts) and all(v["verdict"] in allowed for v in verdicts)
    expected["address_" + "_or_".join(ticket.expected.address)] = stages["address"] in ticket.expected.address
    if ticket.expected.dependency:
        expected[f"declares_{ticket.expected.dependency}"] = _declares(job, final_head, ticket.expected.dependency)
    expected["hidden_tests_pass"] = stages["hidden_tests"] == "passed"

    by_role: dict[str, dict[str, float]] = {}
    turns = 0
    for a in attempts:
        r = by_role.setdefault(a.role, {"attempts": 0, "seconds": 0.0})
        r["attempts"] += 1
        u = a.usage if isinstance(a.usage, dict) else {}
        if a.ended_at is not None:
            r["seconds"] = round(r["seconds"] + (a.ended_at - a.started_at), 1)
        for k, v in (u.get("usage") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                r[k] = r.get(k, 0) + v
        if a.role == "writer" and isinstance(u.get("num_turns"), int):
            turns += u["num_turns"]
    last_verify = verifies[-1].detail if verifies else None
    return {
        "key": ticket.key,
        "kind": ticket.kind,
        "final": "done" if job.state == "READY" else f"blocked: {blocked} at {job.blocked_step}" if blocked else "error",
        "blocked_message": (job.blocked_message or "")[:500] if blocked else None,
        "stages": stages,
        "red_first_valid": bool(job.tester_commit_sha),
        "red_first_verdicts": verdicts,
        "tests": {"acceptance": sum(t["label"] == "acceptance" for t in tests),
                  "guard": sum(t["label"] == "guard" for t in tests),
                  "untestable": len(cmap.get("untestable", []))},
        "final_tests_green": bool(last_verify and last_verify["passed"] and last_verify["head"] == final_head),
        "review_findings": findings,
        "harness_judgements": {v: judgements.count(v) for v in sorted(set(judgements))},
        "address": stages["address"],
        "address_reason": (item.decline_reason if item is not None and item.disposition == "declined" else None),
        "hidden": {"passed": hd["passed"], "total": hd["total"], "failed": hd["failed"], "error": hd.get("error")} if hd else None,
        "expected": expected,
        "metrics": {
            "wall_seconds": round(wall, 1),
            "writer_turns": turns,
            "repair_cycles": job.cycles_used - (1 if seeded else 0),  # local VERIFY repairs (R24), not the seeded packet
            "tester_attempts": tester_attempts,
            "nudges": len(ev("writer_nudge")),
            "fresh_writer_fallbacks": len(ev("fresh_writer")),
            "by_role": by_role,
        },
        "final_head": final_head,
    }


def _verify_stage(events: Sequence[st.Event], blocked: str | None) -> str:
    if events and events[0].detail.get("passed"):
        return "passed"
    if any(e.detail.get("passed") for e in events):
        return "passed_after_repair"
    if blocked:
        return blocked
    return "failed" if events else "not_reached"


def _declares(job: st.Job, head: str | None, package: str) -> bool:
    """Whether requirements.txt at the final head names the package (any version spec)."""
    if not head or not job.clone_path:
        return False
    p = ws_mod.git(Path(job.clone_path), "show", f"{head}:requirements.txt", check=False)
    if p.returncode != 0:
        return False
    name = re.compile(rf"^\s*{re.escape(package)}\s*(?:[\[=<>!~;@ ]|$)", re.IGNORECASE)
    return any(name.match(line) for line in p.stdout.decode(errors="replace").splitlines())


# --- comparing a run with the baseline -----------------------------------------------------------


def _rank(outcome: str) -> int:
    return 2 if outcome in SUCCESS else 1 if outcome in DEGRADED else 0


@dataclass
class Comparison:
    regressions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)  # neither better nor worse: worth knowing
    improvements: list[str] = field(default_factory=list)
    prompts_changed: list[str] = field(default_factory=list)


def compare(baseline: Mapping[str, Any], run: Mapping[str, Any]) -> Comparison:
    """What changed from the baseline to this run. Regressions (exit 2): a stage that succeeded now
    fails, red-first no longer valid, final tests no longer green, fewer hidden tests passing, an
    expected outcome no longer met, a ticket that completed no longer completing. Warnings only:
    a first-try stage now needing a retry or repair, more review findings, fewer tester tests, and
    runs that are clearly longer (wall time or writer turns)."""
    c = Comparison()
    old_p, new_p = baseline.get("prompt_hashes", {}), run.get("prompt_hashes", {})
    c.prompts_changed = sorted(n for n in set(old_p) | set(new_p) if old_p.get(n) != new_p.get(n))
    for what in ("claude", "codex"):
        a, b = (baseline.get("cli") or {}).get(what), (run.get("cli") or {}).get(what)
        if a != b:
            c.changes.append(f"{what} CLI: {a} -> {b}")
    for what in ("writer_model", "codex_model"):
        if baseline.get(what) != run.get(what):
            c.changes.append(f"{what}: {baseline.get(what)} -> {run.get(what)}")
    old = {t["key"]: t for t in baseline.get("tickets", [])}
    new = {t["key"]: t for t in run.get("tickets", [])}
    for key in sorted(set(old) | set(new), key=lambda k: (len(k), k)):
        if key not in new:
            c.regressions.append(f"{key}: not run in this run")
            continue
        if key not in old:
            c.changes.append(f"{key}: new ticket, not in the baseline")
            continue
        _compare_ticket(c, key, old[key], new[key])
    return c


def _compare_ticket(c: Comparison, key: str, a: Mapping[str, Any], b: Mapping[str, Any]) -> None:
    if a["final"] == "done" and b["final"] != "done":
        c.regressions.append(f"{key}: completed in the baseline, now {b['final']}")
    elif a["final"] != "done" and b["final"] == "done":
        c.improvements.append(f"{key}: now completes (baseline: {a['final']})")
    for stage in STAGES:
        sa, sb = a["stages"].get(stage, "not_reached"), b["stages"].get(stage, "not_reached")
        if sa == sb:
            continue
        ra, rb = _rank(sa), _rank(sb)
        line = f"{key} {stage}: {sa} -> {sb}"
        if ra > 0 and rb == 0:
            c.regressions.append(line)
        elif ra == 2 and rb == 1:
            c.warnings.append(line)
        elif rb > ra:
            c.improvements.append(line)
        else:
            c.changes.append(line)
    for flag, label in (("red_first_valid", "red-first valid"), ("final_tests_green", "final tests green")):
        if a.get(flag) and not b.get(flag):
            c.regressions.append(f"{key}: {label} in the baseline, not now")
    ha, hb = a.get("hidden") or {}, b.get("hidden") or {}
    if ha.get("passed", 0) > hb.get("passed", 0):
        c.regressions.append(f"{key}: hidden tests {ha.get('passed')}/{ha.get('total')} -> {hb.get('passed', 0)}/{hb.get('total', ha.get('total'))}"
                             + (f" (failing: {', '.join(hb.get('failed') or [])})" if hb.get("failed") else ""))
    elif hb.get("passed", 0) > ha.get("passed", 0):
        c.improvements.append(f"{key}: hidden tests {ha.get('passed', 0)} -> {hb.get('passed')} passing")
    for name, ok in b.get("expected", {}).items():
        was = a.get("expected", {}).get(name)
        if was and not ok:
            c.regressions.append(f"{key}: expected outcome {name} met in the baseline, not now")
        elif was is False and ok:
            c.improvements.append(f"{key}: expected outcome {name} now met")
    fa, fb = a.get("review_findings"), b.get("review_findings")
    if fa is not None and fb is not None and fb > fa:
        c.warnings.append(f"{key}: Codex review findings {fa} -> {fb}")
    elif fa is not None and fb is not None and fb < fa:
        c.changes.append(f"{key}: Codex review findings {fa} -> {fb}")
    ta, tb = (a.get("tests") or {}).get("acceptance", 0), (b.get("tests") or {}).get("acceptance", 0)
    if tb < ta:
        c.warnings.append(f"{key}: fewer acceptance tests from the tester ({ta} -> {tb})")
    ma, mb = a.get("metrics", {}), b.get("metrics", {})
    wa, wb = ma.get("wall_seconds") or 0, mb.get("wall_seconds") or 0
    if wa and wb > wa * SLOWER_RATIO and wb - wa > SLOWER_SECONDS:
        c.warnings.append(f"{key}: runs longer, {wa:.0f} s -> {wb:.0f} s")
    na, nb = ma.get("writer_turns") or 0, mb.get("writer_turns") or 0
    if na and nb > na * SLOWER_RATIO and nb - na >= MORE_TURNS:
        c.warnings.append(f"{key}: more writer turns, {na} -> {nb}")


def format_comparison(c: Comparison, baseline: Mapping[str, Any]) -> str:
    lines = [f"gaffer eval: compared with baseline {baseline.get('run_id')} (stored {baseline.get('promoted_at') or '?'})"]
    lines.append("  prompts changed since the baseline: " + (", ".join(c.prompts_changed) if c.prompts_changed else "none"))
    for title, items in (("REGRESSION", c.regressions), ("warning", c.warnings), ("improved", c.improvements),
                         ("changed", c.changes)):
        for i in items:
            lines.append(f"  {title}: {i}")
    lines.append(f"gaffer eval: {len(c.regressions)} regression(s), {len(c.warnings)} warning(s).")
    return "\n".join(lines)


def format_ticket(t: Mapping[str, Any]) -> str:
    s = t["stages"]
    hidden = t.get("hidden") or {}
    m = t.get("metrics", {})
    return (f"  {t['key']} ({t['kind']}): {t['final']}\n"
            f"    " + ", ".join(f"{k} {s.get(k)}" for k in STAGES) + "\n"
            f"    red-first valid {t.get('red_first_valid')}; final tests green {t.get('final_tests_green')}; "
            f"review findings {t.get('review_findings')}; hidden {hidden.get('passed', '-')}/{hidden.get('total', '-')}; "
            f"writer turns {m.get('writer_turns')}; {m.get('wall_seconds', 0):.0f} s\n"
            f"    expected: " + ", ".join(f"{k} {'ok' if v else 'MISSED'}" for k, v in t.get("expected", {}).items()))


# --- storage (~/.gaffer/evals) ------------------------------------------------------------------


def evals_home(home: Path) -> Path:
    return rn.gaffer_dir(home) / "evals"


def baseline_path(home: Path) -> Path:
    return evals_home(home) / "baseline.json"


def run_path(home: Path, run_id: str) -> Path:
    return evals_home(home) / "runs" / f"{run_id}.json"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise EvalError(f"{path}: {e}") from None


def load_baseline(home: Path) -> dict[str, Any] | None:
    path = baseline_path(home)
    return _read_json(path) if path.exists() else None


def store_baseline(home: Path, record: Mapping[str, Any], now: float) -> dict[str, Any]:
    base = dict(record)
    base["promoted_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    _write_json(baseline_path(home), base)
    return base


# --- the command --------------------------------------------------------------------------------


@contextlib.contextmanager
def _run_lock(home: Path) -> Iterator[None]:
    """One gaffer at a time: eval shares the subscription and ~/.gaffer with `gaffer run` (R9)."""
    fd = rn._flock(rn.run_lock_path(home), "the run lock", "a gaffer run, doctor or eval is in progress")
    try:
        rn._stamp_lock(fd, "eval")
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def new_run_id(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(3)


def eval_main(config_path: Path | None, *, home: Path, save_baseline: bool = False, promote: str | None = None,
              out: TextIO = sys.stdout, evals_dir: Path = EVALS_DIR,
              children: Callable[[Config, Config | None], rn.ChildrenPort] | None = None,
              scanner: ws_mod.SecretScanner | None = None, clock: rn.Clock | None = None,
              scratch_dir: Path | None = None) -> int:
    """`gaffer eval [--config PATH] [--save-baseline | --promote RUN]`. `children(eval_cfg, doctor_cfg)`,
    `scanner`, `clock` and `scratch_dir` (where the toy repo and job clones go; default the system
    temp dir) are test seams; by default the real children, gitleaks and the system clock."""
    clock = clock or st.SystemClock()
    say = lambda text: print(text, file=out, flush=True)  # noqa: E731
    try:
        if promote is not None:
            return _promote_main(home, promote, clock, say)
        return _eval(config_path, home=home, save_baseline=save_baseline, say=say, evals_dir=evals_dir,
                     children=children, scanner=scanner, clock=clock, scratch_dir=scratch_dir)
    except rn.Refused as e:
        say(f"gaffer eval: refused: {e}")
        return EXIT_REFUSED
    except gconfig.ConfigError as e:
        say(f"gaffer eval: {e}")
        return EXIT_REFUSED
    except (EvalError, st.StoreError, ws_mod.WorkspaceError) as e:
        say(f"gaffer eval: error: {e}")
        return EXIT_ERROR


def _promote_main(home: Path, run_id: str, clock: rn.Clock, say: Callable[[str], None]) -> int:
    if not _RUN_ID.fullmatch(run_id):
        raise rn.Refused(f"{run_id!r} is not a run id like 20261023T101500-a1b2c3 (see ~/.gaffer/evals/runs).")
    path = run_path(home, run_id)
    if not path.exists():
        raise rn.Refused(f"no stored run {run_id} ({path}).")
    record = _read_json(path)
    if not record.get("complete"):
        raise rn.Refused(f"run {run_id} is incomplete ({record.get('stopped') or 'stopped early'}); "
                         f"only a complete run can be the baseline.")
    store_baseline(home, record, clock.wall())
    say(f"gaffer eval: run {run_id} is now the baseline ({baseline_path(home)}).")
    return EXIT_PASS


def _eval(config_path: Path | None, *, home: Path, save_baseline: bool, say: Callable[[str], None], evals_dir: Path,
          children: Callable[[Config, Config | None], rn.ChildrenPort] | None, scanner: ws_mod.SecretScanner | None,
          clock: rn.Clock, scratch_dir: Path | None) -> int:
    tickets = load_tickets(evals_dir / "tickets")
    path = config_path or gconfig.default_config_path(home)
    user = gconfig.load_config(path, home=home) if config_path is not None or path.exists() else None
    if user is None:
        say(f"gaffer eval: no {path}; running under built-in defaults (writer model "
            f"{gconfig.DEFAULT_WRITER_MODEL}, a stand-in vault entry, a temp job root).")
    hashes = prompt_hashes()
    with st.Store(st.default_db_path(home), clock=clock) as doctor_store, _run_lock(home):
        started = clock.wall()
        run_id = new_run_id(started)
        run_dir = evals_home(home) / "runs" / run_id
        run_dir.mkdir(mode=0o700, parents=True)
        # The toy repo, its bare remote and the job clones: outside ~/.gaffer, which children can't read
        # (R28, R30). Kept after the run for inspection.
        scratch = Path(tempfile.mkdtemp(prefix="gaffer-eval-", dir=scratch_dir))
        checkout, remote = make_toy(evals_dir / "toy", scratch)
        cfg = eval_config(user, home=home, checkout=checkout, job_root=scratch / "jobs", scratch=run_dir)
        env = Env(home=home, cfg=cfg, doctor_cfg=user, doctor_store=doctor_store, run_dir=run_dir,
                  remote_url=str(remote), children=(children or EvalChildren)(cfg, user),
                  scanner=scanner or ws_mod.Gitleaks(), tickets=tickets, clock=clock, say=say)
        say(f"gaffer eval: run {run_id}: {len(tickets)} tickets; writer model {cfg.writer_model}; "
            f"logs in {run_dir}; job clones in {scratch / 'jobs'}.")
        results: list[dict[str, Any]] = []
        stopped = None
        cli: dict[str, str] = {}
        for ticket in tickets:
            say(f"gaffer eval: {ticket.key} ({ticket.kind}): running ...")
            tr = run_ticket(env, ticket)
            results.append(tr.result)
            cli = cli or tr.result.get("cli") or {}
            say(format_ticket(tr.result))
            if tr.usage_limit:
                stopped = f"usage limit on {ticket.key}: {tr.result.get('blocked_message') or ''}".strip()
                break
            if tr.exit_code == EXIT_ERROR:
                stopped = f"internal error on {ticket.key}: {tr.result.get('error') or 'see ' + tr.result['log']}"
                break
        record = {
            "version": RECORD_VERSION, "run_id": run_id,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "seconds": round(clock.wall() - started, 1),
            "complete": stopped is None and len(results) == len(tickets),
            "stopped": stopped,
            "prompt_hashes": hashes,
            "cli": cli, "srt": ch.SRT_VERSION,
            "writer_model": cfg.writer_model, "codex_model": cfg.codex_model,
            "config": "defaults" if user is None else str(user.path),
            "tickets": results,
        }
        _write_json(run_path(home, run_id), record)
    say(f"gaffer eval: run recorded in {run_path(home, run_id)}.")
    return _verdict(home, record, save_baseline=save_baseline, say=say, now=clock.wall())


def _verdict(home: Path, record: dict[str, Any], *, save_baseline: bool, say: Callable[[str], None], now: float) -> int:
    baseline = load_baseline(home)
    c = compare(baseline, record) if baseline is not None else None
    if baseline is not None:
        say(format_comparison(c, baseline))
    if not record["complete"]:
        say(f"gaffer eval: INCOMPLETE ({record['stopped']}); not stored as a baseline. "
            f"Rerun `gaffer eval` once the cause is fixed.")
        return EXIT_INCOMPLETE
    if baseline is None or save_baseline:
        store_baseline(home, record, now)
        say(f"gaffer eval: stored run {record['run_id']} as the baseline ({baseline_path(home)})"
            + ("." if baseline is None else ", replacing " + str(baseline.get("run_id")) + "."))
        return EXIT_PASS
    if c is not None and c.regressions:
        say(f"gaffer eval: REGRESSION against baseline {baseline.get('run_id')}. If the change is intended, "
            f"promote this run with `gaffer eval --promote {record['run_id']}`.")
        return EXIT_REGRESSION
    return EXIT_PASS


class EvalChildren(rn.RealChildren):
    """The production children, bound to the eval config. doctor() proves the user's gaffer.toml (or
    the built-in defaults, as `gaffer doctor` does), never the eval config with its temp paths, so a
    pass recorded here is the one `gaffer run` checks and the reverse."""

    def __init__(self, cfg: Config, doctor_cfg: Config | None, paths: ch.Paths | None = None):
        super().__init__(cfg, paths)
        self.doctor_cfg = doctor_cfg

    def doctor(self):
        return rn.doctor_mod.run_doctor(self.doctor_cfg, paths=self.paths)
