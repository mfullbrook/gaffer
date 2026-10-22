"""Job workspace: job clone, verification worktree, installs, tester files,
commit gates, the gitleaks gate, push, PR and the round summary comment.

Decisions: R4 (verification worktree at an exact head), R6 (tester paths),
R7 and R30 (Tester-Change-Reason trailers on tester, harness and CI files),
R9 (round summary marker), R10 (writer gates), R12 and R27 (PR opening, PR
body read-back), R14 and R29 (installs; the sandboxed run is T5's), R15
(gitleaks), R21, R28 and R30 (per-job `git clone --local` under job_root,
commit identity in the clone's config).

Trust boundary: the job dir is the writer's sandbox write root (R21), so
everything in it, the clone's .git included, is writer-controlled. gaffer runs
git there outside any sandbox, so every gaffer git command disables hooks,
fsmonitor, credential helpers and signing, ignores Mark's global and system
git config, and restores the clone's .git/config to the copy gaffer wrote
before it runs (`_guard`). Pushes go to an explicit URL, never `origin`.
"""

from __future__ import annotations

import base64
import bisect
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from gaffer.config import Config, RepoConfig, Secret, matches_any

log = logging.getLogger(__name__)

# --- reasons ------------------------------------------------------------------

# BLOCKED reasons from the ledger.
TESTER_INVALID = "tester_invalid"  # R6
WRITER_DIRTY = "writer_dirty"  # R10
WRITER_NO_RESULT = "writer_no_result"  # R10 (cited SHA mismatch)
WRITER_NO_CHANGE = "writer_no_change"  # design Writer rules, R8
TESTER_COMMIT_REWRITTEN = "tester_commit_rewritten"  # design Writer rules
FORBIDDEN_PATH = "forbidden_path"  # design Writer rules, R30
TESTER_CHANGE_UNEXPLAINED = "tester_change_unexplained"  # R7
SECRET_DETECTED = "secret_detected"  # R15
FOREIGN_HEAD = "foreign_head"  # design External changes
# Not in the ledger; named here and listed in docs/t7-workspace-report.md.
WRITER_BRANCH_MOVED = "writer_branch_moved"
WRITER_HISTORY_REWRITTEN = "writer_history_rewritten"
WORKSPACE_TAMPERED = "workspace_tampered"
SECRET_SCAN_FAILED = "secret_scan_failed"
FOREIGN_PR = "foreign_pr"


class WorkspaceError(Exception):
    """A workspace step failed. `reason` is a BLOCKED reason, or None for an internal error."""

    reason: str | None = None

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class GitError(WorkspaceError):
    pass


class WorkspaceTampered(WorkspaceError):
    reason = WORKSPACE_TAMPERED


class TesterOutputInvalid(WorkspaceError):
    """R6: one retry with the problems attached, then BLOCKED: tester_invalid (runner)."""

    reason = TESTER_INVALID

    def __init__(self, problems: Sequence[str]):
        self.problems = tuple(problems)
        super().__init__("tester output invalid:\n" + "\n".join(f"  - {p}" for p in self.problems))


class SecretScanError(WorkspaceError):
    """gitleaks missing, failed or unreadable. Fails closed: never treated as clean."""

    reason = SECRET_SCAN_FAILED


class SecretDetected(WorkspaceError):
    reason = SECRET_DETECTED

    def __init__(self, result: ScanResult):
        self.result = result
        super().__init__(result.summary())


class PushRejected(WorkspaceError):
    reason = FOREIGN_HEAD


class ForeignPullRequest(WorkspaceError):
    reason = FOREIGN_PR


# --- git ----------------------------------------------------------------------

GIT_TIMEOUT = 300
# Applied to every git command gaffer runs: nothing the writer can put in the
# job clone (hooks, fsmonitor, helpers, signing programs) runs as gaffer.
SAFE_GIT_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.pager=cat",
    "credential.helper=",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "protocol.allow=never",
    "protocol.https.allow=always",
    "protocol.file.allow=always",
)
# R28: the writer can't use `git -c`, so the identity goes in the clone's config.
GIT_USER_NAME = "gaffer"
GIT_USER_EMAIL = "gaffer@gaffer.invalid"
# R31: never hardlink objects; a hardlinked object file is writable through
# the job dir, so a writer could rewrite objects in Mark's checkout.
CLONE_HARDLINKS = False

_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SHORT_SHA = re.compile(r"[0-9a-f]{7,64}")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9]*-\d+")
_GITHUB_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]+")


def _git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_ADVICE": "0",
    }
    if extra:
        env.update(extra)
    return env


def _scrub(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "<redacted>")
    return text


def git(
    cwd: Path,
    *args: str,
    input: bytes | None = None,
    env: Mapping[str, str] | None = None,
    secrets: Sequence[str] = (),
    check: bool = True,
    timeout: int = GIT_TIMEOUT,
) -> subprocess.CompletedProcess[bytes]:
    """Run git with gaffer's safe settings. Arguments never carry a secret; `env` may."""
    argv = ["git"]
    for kv in SAFE_GIT_CONFIG:
        argv += ["-c", kv]
    argv += args
    log.debug("git %s (in %s)", " ".join(args), cwd)
    try:
        p = subprocess.run(argv, cwd=cwd, env=_git_env(env), input=input, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out after {timeout} s in {cwd}") from None
    if check and p.returncode != 0:
        err = p.stderr.decode(errors="replace").strip()[-2000:]
        raise GitError(_scrub(f"git {' '.join(args[:3])} exited {p.returncode} in {cwd}: {err}", secrets))
    return p


def _out(cwd: Path, *args: str, **kw: Any) -> str:
    return git(cwd, *args, **kw).stdout.decode(errors="replace").strip()


def _resolve_commit(cwd: Path, rev: str) -> str | None:
    p = git(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}", check=False)
    return p.stdout.decode().strip() if p.returncode == 0 else None


def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    p = git(cwd, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    if p.returncode in (0, 1):
        return p.returncode == 0
    raise GitError(f"git merge-base --is-ancestor failed: {p.stderr.decode(errors='replace').strip()}")


def github_remote_url(github: str) -> str:
    if not _GITHUB_REPO.fullmatch(github):
        raise ValueError(f"{github!r} is not owner/repo")
    return f"https://github.com/{github}.git"


def _auth_env(url: str, token: Secret | None) -> tuple[dict[str, str], tuple[str, ...]]:
    """git auth for github.com via an http.extraHeader in the environment, never argv."""
    if token is None or not url.startswith("https://github.com/"):
        return {}, ()
    raw = token.reveal()
    basic = base64.b64encode(f"x-access-token:{raw}".encode()).decode()
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }
    return env, (raw, basic)


# --- job layout -----------------------------------------------------------------


def branch_name(issue_key: str, attempt: int = 1) -> str:
    """gaffer/<key>, or gaffer/<key>-<n> after --fresh (R8)."""
    if not _ISSUE_KEY.fullmatch(issue_key):
        raise ValueError(f"{issue_key!r} is not an issue key")
    return f"gaffer/{issue_key}" if attempt == 1 else f"gaffer/{issue_key}-{attempt}"


def job_dir(config: Config, job_id: str) -> Path:
    """config.job_root/<job> (R30). The job id is one safe path component."""
    if not _JOB_ID.fullmatch(job_id) or ".." in job_id:
        raise ValueError(f"{job_id!r} is not a valid job id")
    return config.job_root / job_id


@dataclass
class Workspace:
    """One job's clone. `to_record()` is what the store keeps (T6)."""

    job_id: str
    root: Path
    branch: str
    base_branch: str
    base_sha: str
    remote_url: str
    git_config: str  # the clone's .git/config as gaffer wrote it
    tester_commit: str | None = None
    tester_files: tuple[str, ...] = ()
    # Notes for the report each time gaffer found and reverted a change to .git/config.
    config_restores: list[str] = field(default_factory=list)

    @property
    def clone(self) -> Path:
        return self.root / "repo"

    @property
    def verify(self) -> Path:
        return self.root / "verify"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def composer_home(self) -> Path:
        return self.root / "composer-home"

    @property
    def composer_cache(self) -> Path:
        return self.composer_home / "cache"

    @property
    def npm_cache(self) -> Path:
        return self.root / "npm-cache"

    def to_record(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "root": str(self.root),
            "branch": self.branch,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "remote_url": self.remote_url,
            "git_config": self.git_config,
            "tester_commit": self.tester_commit,
            "tester_files": list(self.tester_files),
        }

    @classmethod
    def from_record(cls, rec: Mapping[str, Any]) -> Workspace:
        ws = cls(
            job_id=rec["job_id"],
            root=Path(rec["root"]),
            branch=rec["branch"],
            base_branch=rec["base_branch"],
            base_sha=rec["base_sha"],
            remote_url=rec["remote_url"],
            git_config=rec["git_config"],
            tester_commit=rec.get("tester_commit"),
            tester_files=tuple(rec.get("tester_files") or ()),
        )
        _guard(ws)
        return ws


def create_workspace(
    config: Config,
    repo: RepoConfig,
    job_id: str,
    branch: str,
    *,
    token: Secret | None = None,
    remote_url: str | None = None,
    fetch_base: bool = True,
    user_name: str = GIT_USER_NAME,
    user_email: str = GIT_USER_EMAIL,
) -> Workspace:
    """Clone Mark's checkout into job_root/<job>/repo and branch from the base (R21, R28, R30).

    origin is the GitHub URL (or `remote_url`, which tests point at a bare
    repo). With `fetch_base`, the base branch is fetched from origin first so
    the job doesn't start from a stale local branch; its SHA is recorded.
    """
    root = job_dir(config, job_id)
    url = remote_url or github_remote_url(repo.github)
    if root.exists() or root.is_symlink():
        raise WorkspaceError(f"job dir {root} already exists; reuse it with Workspace.from_record or remove it")
    p = git(Path("/"), "check-ref-format", "--branch", branch, check=False)
    if p.returncode != 0 or not branch.startswith("gaffer/"):
        raise ValueError(f"{branch!r} is not a valid gaffer branch name")
    config.job_root.mkdir(parents=True, exist_ok=True)
    root.mkdir()
    for d in ("tmp",):
        (root / d).mkdir()
    if "php" in repo.ecosystems:
        (root / "composer-home" / "cache").mkdir(parents=True)

    clone_args = ["clone", "--quiet", "--no-checkout", "--local"]
    if not CLONE_HARDLINKS:
        clone_args.append("--no-hardlinks")
    git(root, *clone_args, "--", str(repo.path), "repo")
    clone = root / "repo"
    git(clone, "remote", "set-url", "origin", url)
    git(clone, "config", "user.name", user_name)
    git(clone, "config", "user.email", user_email)
    base_ref = f"refs/remotes/origin/{repo.base}"
    if fetch_base:
        env, secrets = _auth_env(url, token)
        git(clone, "fetch", "--quiet", "--no-tags", "origin", f"+refs/heads/{repo.base}:{base_ref}", env=env, secrets=secrets)
    base_sha = _resolve_commit(clone, base_ref)
    if base_sha is None:
        raise WorkspaceError(f"base branch {repo.base!r} not found in {url if fetch_base else repo.path}")
    git(clone, "checkout", "--quiet", "--no-track", "-f", "-B", branch, base_sha)
    ws = Workspace(
        job_id=job_id,
        root=root,
        branch=branch,
        base_branch=repo.base,
        base_sha=base_sha,
        remote_url=url,
        git_config=(clone / ".git" / "config").read_text(),
    )
    log.info("job %s: clone at %s, branch %s, base %s", job_id, clone, branch, base_sha)
    return ws


def _guard(ws: Workspace) -> None:
    """Refuse a replaced .git; put back gaffer's .git/config if the writer changed it."""
    for p in (ws.root, ws.clone, ws.clone / ".git"):
        if p.is_symlink():
            raise WorkspaceTampered(f"{p} is a symlink")
    if not (ws.clone / ".git").is_dir():
        raise WorkspaceTampered(f"{ws.clone / '.git'} is not a directory")
    cfg = ws.clone / ".git" / "config"
    try:
        current = None if cfg.is_symlink() else cfg.read_text(errors="replace")
    except FileNotFoundError:
        current = None
    if current == ws.git_config:
        return
    note = f"{cfg} was changed outside gaffer; restored gaffer's copy"
    log.warning("job %s: %s", ws.job_id, note)
    ws.config_restores.append(note)
    tmp = cfg.with_name(f"config.gaffer-{os.getpid()}")
    tmp.unlink(missing_ok=True)
    tmp.write_text(ws.git_config)
    os.replace(tmp, cfg)


# --- installs (R14, R29) --------------------------------------------------------

DEFAULT_INSTALL_TIMEOUT = 15 * 60


@dataclass(frozen=True)
class InstallRequest:
    """What T5's sandboxed runner needs to run install_command once.

    Writes are limited to `write_root` (the job dir); `allowed_hosts` empty
    means no network. `env` carries R29's Composer variables and the TMPDIR to
    set inside the wrapped command (srt itself keeps the short system TMPDIR).
    """

    command: str
    cwd: Path
    write_root: Path
    allowed_hosts: tuple[str, ...]
    env: Mapping[str, str]
    timeout: int = DEFAULT_INSTALL_TIMEOUT


@dataclass(frozen=True)
class InstallResult:
    exit_code: int
    output_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class InstallRunner(Protocol):
    """Implemented by children.py (T5): runs the request under sandbox-runtime."""

    def __call__(self, request: InstallRequest) -> InstallResult: ...


def install_env(ws: Workspace, repo: RepoConfig) -> dict[str, str]:
    env = {"TMPDIR": str(ws.tmp)}
    if "php" in repo.ecosystems:  # R29
        env |= {
            "COMPOSER_HOME": str(ws.composer_home),
            "COMPOSER_CACHE_DIR": str(ws.composer_cache),
            "COMPOSER_NO_INTERACTION": "1",
        }
    if "node" in repo.ecosystems:  # spike: npm's cache must sit inside the job dir
        env["npm_config_cache"] = str(ws.npm_cache)
    return env


def install_request(ws: Workspace, repo: RepoConfig, checkout: Path, *, offline: bool) -> InstallRequest | None:
    """None when the repo has no install_command."""
    if not repo.install_command:
        return None
    if checkout not in (ws.clone, ws.verify):
        raise ValueError(f"{checkout} is not this job's clone or verification worktree")
    return InstallRequest(
        command=repo.install_command,
        cwd=checkout,
        write_root=ws.root,
        allowed_hosts=() if offline else repo.network_allowlist,
        env=install_env(ws, repo),
    )


def run_install(ws: Workspace, repo: RepoConfig, checkout: Path, runner: InstallRunner, *, offline: bool = False) -> InstallResult | None:
    """Run install_command in the job clone (online, warms the cache) or a verification worktree."""
    req = install_request(ws, repo, checkout, offline=offline)
    if req is None:
        return None
    log.info("job %s: install in %s (%s)", ws.job_id, checkout, "offline" if offline else ", ".join(req.allowed_hosts) or "no hosts")
    return runner(req)


# --- verification worktree (R4, R29) ----------------------------------------------


@dataclass(frozen=True)
class VerificationCheckout:
    path: Path
    head: str
    install: InstallResult | None


def prepare_verification(
    ws: Workspace, repo: RepoConfig, head: str, *, install: InstallRunner | None = None
) -> VerificationCheckout:
    """A fresh detached worktree of the job clone at exactly `head`, inside the job dir.

    Each verification gets its own install_command run: for PHP offline from the
    job's warm Composer cache (R29, a symlinked vendor/ loads the wrong src/),
    otherwise with the repo's registry hosts (R14).
    """
    _guard(ws)
    if not _FULL_SHA.fullmatch(head) or _resolve_commit(ws.clone, head) != head:
        raise WorkspaceError(f"{head!r} is not a full commit SHA in the job clone")
    if ws.verify.is_symlink():
        ws.verify.unlink()
    if ws.verify.exists():
        git(ws.clone, "worktree", "remove", "--force", str(ws.verify), check=False)
        if ws.verify.exists():
            shutil.rmtree(ws.verify)
    git(ws.clone, "worktree", "prune")
    git(ws.clone, "worktree", "add", "--quiet", "--detach", str(ws.verify), head)
    actual = _out(ws.verify, "rev-parse", "HEAD")
    if actual != head:
        raise WorkspaceError(f"verification worktree is at {actual}, expected {head}")
    if _out(ws.verify, "status", "--porcelain", "--untracked-files=all"):
        raise WorkspaceError("verification worktree is not clean after checkout")
    result = None
    if install is not None:
        result = run_install(ws, repo, ws.verify, install, offline="php" in repo.ecosystems)
    return VerificationCheckout(path=ws.verify, head=head, install=result)


# --- tester files (R6) -------------------------------------------------------------

MAX_TESTER_FILES = 20
MAX_TESTER_FILE_BYTES = 200 * 1024


@dataclass(frozen=True)
class TesterFile:
    path: str
    content: str


@dataclass(frozen=True)
class TesterWrite:
    created: tuple[str, ...]
    modified: tuple[str, ...]  # R6: listed in the report

    @property
    def paths(self) -> tuple[str, ...]:
        return self.created + self.modified


def _path_problem(raw: object, repo: RepoConfig) -> str | None:
    if not isinstance(raw, str) or not raw:
        return f"{raw!r}: path must be a non-empty string"
    if "\x00" in raw or "\\" in raw:
        return f"{raw!r}: path contains NUL or a backslash"
    if raw.startswith(("/", "~")):
        return f"{raw!r}: path must be relative to the repo"
    parts = raw.split("/")
    if ".." in parts:
        return f"{raw!r}: '..' is not allowed"
    if any(p in ("", ".") for p in parts):
        return f"{raw!r}: path has an empty or '.' component"
    if any(p.casefold() == ".git" for p in parts):
        return f"{raw!r}: path enters .git"
    pp = PurePosixPath(raw)
    if not any(pp.is_relative_to(td) and pp != PurePosixPath(td) for td in repo.test_dirs):
        return f"{raw!r}: not inside a test dir ({', '.join(repo.test_dirs)})"
    if repo.is_forbidden_path(raw):
        return f"{raw!r}: forbidden path"
    return None


def _fs_problem(root: Path, raw: str, repo: RepoConfig) -> tuple[str | None, bool]:
    """(problem, exists). No component may be a symlink; the target must be a plain single-link file."""
    parts = raw.split("/")
    cur = root
    for i, part in enumerate(parts):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            break
        last = i == len(parts) - 1
        if stat.S_ISLNK(st.st_mode):
            return f"{raw!r}: {'/'.join(parts[: i + 1])} is a symlink", False
        if not last and not stat.S_ISDIR(st.st_mode):
            return f"{raw!r}: {'/'.join(parts[: i + 1])} is not a directory", False
        if last:
            if not stat.S_ISREG(st.st_mode):
                return f"{raw!r}: exists and is not a regular file", False
            if st.st_nlink > 1:
                return f"{raw!r}: existing file has other hard links", False
            break
    real = Path(os.path.realpath(root / raw))
    inside_dir = any(real.is_relative_to(Path(os.path.realpath(root / td))) for td in repo.test_dirs)
    if not real.is_relative_to(root) or not inside_dir:
        return f"{raw!r}: resolves outside the test dirs", False
    return None, os.path.lexists(root / raw)


def validate_tester_files(ws: Workspace, repo: RepoConfig, files: Sequence[TesterFile]) -> list[tuple[TesterFile, bool]]:
    """Every R6 rule, checked before anything is written. Returns (file, exists) pairs."""
    problems: list[str] = []
    if len(files) > MAX_TESTER_FILES:
        problems.append(f"{len(files)} files; at most {MAX_TESTER_FILES} are allowed")
    if not files:
        problems.append("no test files")
    root = Path(os.path.realpath(ws.clone))
    seen: set[str] = set()
    checked: list[tuple[TesterFile, bool]] = []
    for f in files:
        if not isinstance(f, TesterFile):
            problems.append(f"{f!r}: not a tester file entry")
            continue
        if (problem := _path_problem(f.path, repo)) is not None:
            problems.append(problem)
            continue
        if f.path.casefold() in seen:  # APFS is case-insensitive
            problems.append(f"{f.path!r}: duplicate path")
            continue
        seen.add(f.path.casefold())
        if not isinstance(f.content, str):
            problems.append(f"{f.path!r}: content must be text")
            continue
        size = len(f.content.encode())
        if size > MAX_TESTER_FILE_BYTES:
            problems.append(f"{f.path!r}: {size} bytes; at most {MAX_TESTER_FILE_BYTES} are allowed")
            continue
        problem, exists = _fs_problem(root, f.path, repo)
        if problem is not None:
            problems.append(problem)
            continue
        checked.append((f, exists))
    if problems:
        raise TesterOutputInvalid(problems)
    return checked


def write_tester_files(ws: Workspace, repo: RepoConfig, files: Sequence[TesterFile]) -> TesterWrite:
    """Validate all, then write all (R6). Nothing is written if any file is invalid."""
    _guard(ws)
    checked = validate_tester_files(ws, repo, files)
    root = Path(os.path.realpath(ws.clone))
    created: list[str] = []
    modified: list[str] = []
    for f, exists in checked:
        parent = root
        for part in f.path.split("/")[:-1]:
            parent = parent / part
            try:
                os.mkdir(parent)
            except FileExistsError:
                pass
            if parent.is_symlink() or not parent.is_dir():
                raise TesterOutputInvalid([f"{f.path!r}: {parent} changed while writing"])
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(root / f.path, flags, 0o644)
        try:
            if os.fstat(fd).st_nlink > 1:
                raise TesterOutputInvalid([f"{f.path!r}: existing file has other hard links"])
            os.write(fd, f.content.encode())
        finally:
            os.close(fd)
        (modified if exists else created).append(f.path)
    return TesterWrite(created=tuple(created), modified=tuple(modified))


def commit_tester_files(ws: Workspace, write: TesterWrite, message: str) -> str:
    """Commit the tester's files as the branch's first commit; records tester_commit."""
    _guard(ws)
    head = _out(ws.clone, "rev-parse", "HEAD")
    if head != ws.base_sha:
        raise WorkspaceError(f"the tester commit must sit on the base {ws.base_sha}; HEAD is {head}")
    status = _status_paths(ws.clone)
    stray = sorted(set(status) - set(write.paths))
    if stray:
        raise WorkspaceError(f"job clone has changes besides the tester files: {stray}")
    git(ws.clone, "add", "--", *write.paths)
    if git(ws.clone, "diff", "--cached", "--quiet", check=False).returncode == 0:
        raise TesterOutputInvalid(["the tester's files don't change anything"])
    git(ws.clone, "commit", "--quiet", "--no-verify", "-F", "-", input=message.encode())
    ws.tester_commit = _out(ws.clone, "rev-parse", "HEAD")
    ws.tester_files = write.paths
    return ws.tester_commit


# --- writer gates (R7, R10, R30) ------------------------------------------------------

NUDGE_PROMPT = "commit or discard and return the result"  # R10
TRAILER_KEY = "Tester-Change-Reason"  # R7
CI_PATHS = (".github/workflows/**", ".github/actions/**")  # R30: listed prominently


class GateAction(StrEnum):
    OK = "ok"
    NUDGE = "nudge"  # runner resumes the writer once with NUDGE_PROMPT, then calls again with nudged=True
    BLOCKED = "blocked"


@dataclass(frozen=True)
class GuardedChange:
    """A writer change to a tester, harness or CI file: listed in the report, judged by the Codex reviewer (R7)."""

    commit: str
    path: str
    kind: str  # "tester" | "harness" | "ci"
    reason: str | None


@dataclass(frozen=True)
class GateResult:
    action: GateAction
    reason: str | None = None
    message: str = ""
    head: str | None = None
    new_commits: tuple[str, ...] = ()
    guarded_changes: tuple[GuardedChange, ...] = ()
    cited: Mapping[str, str] = field(default_factory=dict)  # as cited -> full SHA
    dirty: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.action is GateAction.OK


def _status_paths(cwd: Path) -> list[str]:
    raw = git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none").stdout.decode(errors="replace")
    tokens = raw.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if len(t) > 3:
            paths.append(t[3:])
            if t[0] in "RC":  # the next token is the original path
                i += 1
                if i < len(tokens) and tokens[i]:
                    paths.append(tokens[i])
        i += 1
    return paths


def _changed_paths(cwd: Path, commit: str) -> list[str]:
    parents = _out(cwd, "rev-list", "--parents", "-n", "1", commit).split()[1:]
    if not parents:
        raise GitError(f"{commit} has no parent")
    out = git(cwd, "diff-tree", "-r", "--no-commit-id", "--name-only", "-z", "--no-renames", parents[0], commit).stdout
    return [p for p in out.decode(errors="replace").split("\0") if p]


def _trailer(cwd: Path, commit: str) -> str | None:
    out = _out(cwd, "log", "-1", f"--format=%(trailers:key={TRAILER_KEY},valueonly,separator=%x1f)", commit)
    values = [v.strip() for v in out.split("\x1f") if v.strip()]
    return "; ".join(values) or None


def _blocked(reason: str, message: str, **kw: Any) -> GateResult:
    return GateResult(action=GateAction.BLOCKED, reason=reason, message=message, **kw)


def check_writer_result(
    ws: Workspace,
    repo: RepoConfig,
    *,
    previous_head: str,
    cited_shas: Iterable[str] = (),
    nudged: bool = False,
    require_new_commit: bool = True,
) -> GateResult:
    """The R10 gates after every WRITER and RESUME, plus the design's Writer rules.

    `previous_head` is the head the writer started from: the tester commit
    after WRITER, the packet's head after RESUME. `require_new_commit` is False
    for a resume whose items were all declined (R8).
    """
    _guard(ws)
    if ws.tester_commit is None:
        raise WorkspaceError("no tester commit recorded for this job")
    dirty = tuple(_status_paths(ws.clone))
    if dirty:
        if not nudged:
            return GateResult(action=GateAction.NUDGE, message=NUDGE_PROMPT, dirty=dirty)
        return _blocked(WRITER_DIRTY, f"uncommitted changes after the nudge: {', '.join(dirty[:20])}", dirty=dirty)

    ref = f"refs/heads/{ws.branch}"
    symref = git(ws.clone, "symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()
    if symref != ref:
        return _blocked(WRITER_BRANCH_MOVED, f"HEAD is {symref or 'detached'}, not {ref}")
    head = _resolve_commit(ws.clone, ref)
    if head is None:
        return _blocked(WRITER_BRANCH_MOVED, f"{ref} does not resolve to a commit")
    if _resolve_commit(ws.clone, ws.tester_commit) is None or not _is_ancestor(ws.clone, ws.tester_commit, head):
        return _blocked(TESTER_COMMIT_REWRITTEN, f"tester commit {ws.tester_commit} is not an ancestor of {head}", head=head)
    if _resolve_commit(ws.clone, previous_head) is None or not _is_ancestor(ws.clone, previous_head, head):
        return _blocked(WRITER_HISTORY_REWRITTEN, f"{previous_head} is not an ancestor of {head}; gaffer never force-pushes", head=head)

    new_commits = tuple(c for c in _out(ws.clone, "rev-list", "--reverse", f"{previous_head}..{head}").split() if c)
    cited_list = list(cited_shas)
    if not new_commits and (require_new_commit or cited_list):
        return _blocked(WRITER_NO_CHANGE, f"no commits after {previous_head}", head=head)

    tester_files = set(ws.tester_files)
    forbidden: list[str] = []
    unexplained: list[str] = []
    guarded: list[GuardedChange] = []
    for c in new_commits:
        reason: str | None = None
        reason_read = False
        for path in _changed_paths(ws.clone, c):
            if repo.is_forbidden_path(path):
                forbidden.append(f"{path} ({c[:12]})")
                continue
            if matches_any(path, CI_PATHS):
                kind = "ci"
            elif path in tester_files:
                kind = "tester"
            elif repo.is_harness_path(path):
                kind = "harness"
            else:
                continue
            if not reason_read:
                reason, reason_read = _trailer(ws.clone, c), True
            guarded.append(GuardedChange(commit=c, path=path, kind=kind, reason=reason))
            if reason is None:
                unexplained.append(f"{path} ({c[:12]})")
    common = {"head": head, "new_commits": new_commits, "guarded_changes": tuple(guarded)}
    if forbidden:
        return _blocked(FORBIDDEN_PATH, "writer commits touch forbidden paths: " + ", ".join(forbidden), **common)
    if unexplained:
        return _blocked(TESTER_CHANGE_UNEXPLAINED, f"no {TRAILER_KEY}: trailer for: " + ", ".join(unexplained), **common)

    cited: dict[str, str] = {}
    bad: list[str] = []
    new_set = set(new_commits)
    for sha in cited_list:
        full = _resolve_commit(ws.clone, sha) if isinstance(sha, str) and _SHORT_SHA.fullmatch(sha) else None
        if full is None or full not in new_set:
            bad.append(repr(sha))
        else:
            cited[sha] = full
    if bad:
        return _blocked(WRITER_NO_RESULT, f"cited SHAs are not new commits since {previous_head[:12]}: {', '.join(bad)}", **common)
    return GateResult(action=GateAction.OK, cited=cited, **common)


# --- secret scan (R15) ---------------------------------------------------------------


@dataclass(frozen=True)
class SecretFinding:
    """A redacted finding: never the secret or the matched text."""

    source: str
    rule_id: str
    description: str
    location: str

    def __str__(self) -> str:
        return f"{self.source}: {self.rule_id} at {self.location} ({self.description}) [secret redacted]"


@dataclass(frozen=True)
class ScanResult:
    findings: tuple[SecretFinding, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        if self.clean:
            return "no secrets found"
        return f"{len(self.findings)} secret(s) found:\n" + "\n".join(f"  - {f}" for f in self.findings)

    def require_clean(self) -> ScanResult:
        if not self.clean:
            raise SecretDetected(self)
        return self


@dataclass(frozen=True)
class RawFinding:
    rule_id: str
    description: str
    line: int


class SecretScanner(Protocol):
    def scan(self, text: str) -> list[RawFinding]: ...


_SAFE_TEXT = re.compile(r"[^A-Za-z0-9 _.:/()-]")


class Gitleaks:
    """gitleaks over stdin, run from an empty temp dir with a minimal environment.

    Running over stdin (rather than `gitleaks git` in the job clone) means a
    writer-planted .gitleaks.toml or .gitleaksignore can't apply, and gitleaks
    never runs git in the writer-controlled clone. `--ignore-gitleaks-allow`
    stops a `gitleaks:allow` comment from hiding a line.
    """

    LEAKS_EXIT = 42

    def __init__(self, binary: str | os.PathLike[str] | None = None, *, timeout: int = 300):
        self.binary = str(binary) if binary is not None else shutil.which("gitleaks")
        self.timeout = timeout

    @staticmethod
    def available() -> bool:
        return shutil.which("gitleaks") is not None

    def scan(self, text: str) -> list[RawFinding]:
        if not self.binary or not os.access(self.binary, os.X_OK):
            raise SecretScanError("gitleaks is not installed (brew install gitleaks); nothing is published without a scan")
        with tempfile.TemporaryDirectory(prefix="gaffer-gitleaks-") as d:
            report = Path(d) / "report.json"
            argv = [
                self.binary, "stdin",
                "--no-banner", "--no-color", "--log-level", "error",
                "--redact", "--ignore-gitleaks-allow",
                "--report-format", "json", "--report-path", str(report),
                "--exit-code", str(self.LEAKS_EXIT),
            ]
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": d, "LC_ALL": "C"}
            try:
                p = subprocess.run(argv, cwd=d, env=env, input=text.encode(), capture_output=True, timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired:
                raise SecretScanError(f"gitleaks timed out after {self.timeout} s") from None
            except OSError as e:
                raise SecretScanError(f"gitleaks could not start: {e.strerror}") from None
            if p.returncode not in (0, self.LEAKS_EXIT):
                # Output deliberately left out: it may quote the scanned text.
                raise SecretScanError(f"gitleaks exited {p.returncode}")
            try:
                data = json.loads(report.read_text()) if report.exists() else None
            except (OSError, ValueError):
                data = None
        if data is None and p.returncode == 0:
            data = []
        if not isinstance(data, list):
            raise SecretScanError("gitleaks wrote no readable report")
        findings = []
        for item in data:
            if not isinstance(item, dict):
                raise SecretScanError("gitleaks report has an unexpected shape")
            line = item.get("StartLine")
            findings.append(RawFinding(
                rule_id=_SAFE_TEXT.sub("", str(item.get("RuleID") or "unknown"))[:80],
                description=_SAFE_TEXT.sub("", str(item.get("Description") or ""))[:160],
                line=line if isinstance(line, int) and not isinstance(line, bool) else 0,
            ))
        if p.returncode == self.LEAKS_EXIT and not findings:
            raise SecretScanError("gitleaks reported leaks but listed none")
        return findings


def scan_texts(scanner: SecretScanner, items: Sequence[tuple[str, str]], *, source: str) -> ScanResult:
    """Scan labelled texts in one gitleaks run: comment bodies, PR text, packet items (R13, R15, R24)."""
    starts: list[int] = []
    labels: list[str] = []
    chunks: list[str] = []
    line = 1
    for label, text in items:
        text = text if text.endswith("\n") else text + "\n"
        starts.append(line)
        labels.append(label)
        chunks.append(text)
        line += text.count("\n")
    if not chunks:
        return ScanResult()

    def locate(n: int) -> str:
        i = max(bisect.bisect_right(starts, n) - 1, 0)
        return f"{labels[i]} line {max(n - starts[i] + 1, 1)}"

    return ScanResult(tuple(
        SecretFinding(source=source, rule_id=f.rule_id, description=f.description, location=locate(f.line))
        for f in scanner.scan("".join(chunks))
    ))


def push_patch(ws: Workspace, head: str, since: str | None = None) -> str:
    """Every commit's message and patch from `since` (default: base) to head.

    Per commit, not the net diff: a secret added then removed is still pushed.
    """
    rng = f"{since or ws.base_sha}..{head}"
    return git(
        ws.clone, "log", "-p", "--reverse", "--no-color", "--no-textconv", "--no-ext-diff", "--no-renames",
        "--diff-merges=first-parent", "--format=commit %H%n%B", rng, "--",
    ).stdout.decode(errors="replace")


def scan_push(ws: Workspace, head: str, scanner: SecretScanner) -> ScanResult:
    """gitleaks over what the push adds: commit messages and added lines.

    Context and removed lines are blanked (keeping line numbers), so a secret
    already on the base doesn't block a job that edits near it.
    """
    _guard(ws)
    text = push_patch(ws, head)
    # Map each line of the patch back to its commit and file for the report.
    marks: list[tuple[int, str, str]] = []
    kept: list[str] = []
    commit, path, in_message = "", "(message)", False
    for n, line in enumerate(text.split("\n"), start=1):
        keep = ""
        if line.startswith("commit ") and _FULL_SHA.fullmatch(line[7:]):
            commit, path, in_message = line[7:], "(message)", True
            marks.append((n, commit, path))
        elif line.startswith("diff --git "):
            in_message = False
        elif in_message:
            keep = line
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
            marks.append((n, commit, path))
        elif line.startswith("+"):
            keep = line[1:]
        kept.append(keep)
    text = "\n".join(kept)
    lines = [m[0] for m in marks]

    def locate(n: int) -> str:
        i = bisect.bisect_right(lines, n) - 1
        if i < 0:
            return f"line {n}"
        _, c, p = marks[i]
        return f"commit {c[:12]} {p}"

    return ScanResult(tuple(
        SecretFinding(source="push diff", rule_id=f.rule_id, description=f.description, location=locate(f.line))
        for f in scanner.scan(text)
    ))


# --- push ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PushResult:
    head: str
    remote_before: str | None
    pushed: bool  # False when the remote already had head (idempotent rerun)
    scan: ScanResult


def remote_head(ws: Workspace, *, token: Secret | None = None) -> str | None:
    ref = f"refs/heads/{ws.branch}"
    env, secrets = _auth_env(ws.remote_url, token)
    out = _out(ws.clone, "ls-remote", "--refs", ws.remote_url, ref, env=env, secrets=secrets)
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name == ref:
            return sha
    return None


def push(
    ws: Workspace,
    head: str,
    *,
    scanner: SecretScanner,
    expected_remote: str | None,
    token: Secret | None = None,
) -> PushResult:
    """gitleaks over every pushed commit, then a fast-forward push of exactly `head`. Never force.

    `expected_remote` is the head gaffer last pushed (None before the first
    push). Anything else on the remote is BLOCKED: foreign_head.
    """
    _guard(ws)
    if not _FULL_SHA.fullmatch(head) or _resolve_commit(ws.clone, head) != head:
        raise WorkspaceError(f"{head!r} is not a full commit SHA in the job clone")
    if ws.tester_commit is None or not _is_ancestor(ws.clone, ws.tester_commit, head):
        raise WorkspaceError(f"{head} does not contain the tester commit", reason=TESTER_COMMIT_REWRITTEN)
    before = remote_head(ws, token=token)
    if before == head:
        return PushResult(head=head, remote_before=before, pushed=False, scan=ScanResult())
    if before != expected_remote:
        raise PushRejected(f"remote {ws.branch} is at {before or 'nothing'}, expected {expected_remote or 'nothing'}")
    scan = scan_push(ws, head, scanner).require_clean()
    env, secrets = _auth_env(ws.remote_url, token)
    refspec = f"{head}:refs/heads/{ws.branch}"  # no '+': never force (design PUBLISH)
    p = git(ws.clone, "push", "--porcelain", "--no-verify", ws.remote_url, refspec, env=env, secrets=secrets, check=False)
    if p.returncode != 0:
        out = _scrub(p.stdout.decode(errors="replace") + p.stderr.decode(errors="replace"), secrets)
        if any(line.startswith("!") for line in out.splitlines()) or "rejected" in out:
            raise PushRejected(f"push of {head} to {ws.branch} was rejected: {out.strip()[-1000:]}")
        raise GitError(f"push of {head} to {ws.branch} failed: {out.strip()[-1000:]}")
    after = remote_head(ws, token=token)
    if after != head:
        raise GitError(f"after push the remote {ws.branch} is at {after}, expected {head}")
    log.info("job %s: pushed %s to %s", ws.job_id, head, ws.branch)
    return PushResult(head=head, remote_before=before, pushed=True, scan=scan)


# --- GitHub REST -----------------------------------------------------------------------

API_URL = "https://api.github.com"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> HttpResponse:
    """The real transport. Authorization is never sent to another host on a redirect."""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        if k.lower() == "authorization":
            req.add_unredirected_header(k, v)
        else:
            req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return HttpResponse(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read())


class GitHubError(WorkspaceError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class GitHubClient:
    """A small REST client for one repo. The token is never logged or shown in errors."""

    def __init__(self, github: str, token: Secret, transport: Transport = urllib_transport, *, api: str = API_URL, timeout: float = 30):
        if not _GITHUB_REPO.fullmatch(github):
            raise ValueError(f"{github!r} is not owner/repo")
        if not isinstance(token, Secret):
            raise TypeError("token must be a config.Secret")
        self.github = github
        self.owner = github.split("/")[0]
        self._token = token
        self._transport = transport
        self._api = api.rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"GitHubClient({self.github!r})"

    def _url(self, path: str, params: Mapping[str, str | int] | None = None) -> str:
        url = path if path.startswith(("http://", "https://")) else f"{self._api}{path}"
        if not url.startswith(self._api + "/"):
            raise GitHubError(f"refusing a request off the API host: {url.split('?')[0]}")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def request(self, method: str, path: str, *, params: Mapping[str, str | int] | None = None, payload: Any = None) -> tuple[Any, Mapping[str, str]]:
        url = self._url(path, params)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gaffer",
            "Authorization": f"Bearer {self._token.reveal()}",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        log.debug("GitHub %s %s", method, url.split("?")[0])
        try:
            resp = self._transport(method, url, headers, body, self._timeout)
        except (OSError, urllib.error.URLError) as e:
            raise GitHubError(f"GitHub {method} {url.split('?')[0]} failed: {_scrub(str(e), [self._token.reveal()])}") from None
        secrets = [self._token.reveal()]
        try:
            data = json.loads(resp.body) if resp.body else None
        except ValueError:
            data = None
        if resp.status >= 400 or (resp.body and data is None):
            msg = data.get("message") if isinstance(data, dict) else None
            text = _scrub(str(msg or resp.body[:200].decode(errors="replace")), secrets)
            raise GitHubError(f"GitHub {method} {url.split('?')[0]} returned {resp.status}: {text}", resp.status)
        return data, {k.lower(): v for k, v in resp.headers.items()}

    def paginate(self, path: str, params: Mapping[str, str | int] | None = None) -> list[Any]:
        items: list[Any] = []
        url: str | None = self._url(path, {**(params or {}), "per_page": 100})
        while url:
            data, headers = self.request("GET", url)
            if not isinstance(data, list):
                raise GitHubError(f"GitHub GET {path} did not return a list")
            items.extend(data)
            m = _LINK_NEXT.search(headers.get("link", ""))
            url = m.group(1) if m else None
        return items

    def authenticated_login(self) -> str:
        data, _ = self.request("GET", "/user")
        return data["login"]

    def open_pulls_for_branch(self, branch: str) -> list[dict[str, Any]]:
        return self.paginate(f"/repos/{self.github}/pulls", {"head": f"{self.owner}:{branch}", "state": "open"})

    def get_pull(self, number: int) -> dict[str, Any]:
        data, _ = self.request("GET", f"/repos/{self.github}/pulls/{int(number)}")
        return data

    def create_pull(self, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        payload = {"title": title, "head": head, "base": base, "body": body, "draft": False}
        data, _ = self.request("POST", f"/repos/{self.github}/pulls", payload=payload)
        return data

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        return self.paginate(f"/repos/{self.github}/issues/{int(number)}/comments")

    def create_issue_comment(self, number: int, body: str) -> dict[str, Any]:
        data, _ = self.request("POST", f"/repos/{self.github}/issues/{int(number)}/comments", payload={"body": body})
        return data


# --- PR (R12, R27) --------------------------------------------------------------------

JOB_MARKER = "gaffer-job: "
_JOB_MARKER_LINE = re.compile(r"^gaffer-job: (\S+)[ \t]*$", re.MULTILINE)
_CR_NOTES_START = re.compile(r"<!--\s*This is an auto-generated comment: release notes by coderabbit\.ai\s*-->")
_CR_NOTES_END = re.compile(r"<!--\s*end of auto-generated comment: release notes by coderabbit\.ai\s*-->")


@dataclass(frozen=True)
class CriterionRow:
    """One line of the PR's criterion map: a criterion and the tester tests that prove it."""

    criterion: str
    tests: tuple[str, ...]


def pr_title(issue_key: str, issue_title: str) -> str:
    """`<KEY>: <issue title>` on one line (R12)."""
    if not _ISSUE_KEY.fullmatch(issue_key):
        raise ValueError(f"{issue_key!r} is not an issue key")
    title = " ".join(issue_title.split())
    return f"{issue_key}: {title}"[:256]


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def pr_body(job_id: str, criteria: Sequence[CriterionRow], untestable: Sequence[str] = ()) -> str:
    """The criterion map and the `gaffer-job: <id>` marker (R12). R23 is unresolved, so nothing else."""
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError(f"{job_id!r} is not a valid job id")
    lines = ["## Criterion map", "", "| # | Criterion | Tester tests |", "|---|---|---|"]
    for i, row in enumerate(criteria, start=1):
        tests = "<br>".join(f"`{_cell(t).replace('`', '')}`" for t in row.tests) or "none"
        lines.append(f"| {i} | {_cell(row.criterion)} | {tests} |")
    if untestable:
        lines += ["", "Untestable, so unverified:", ""]
        lines += [f"- {_cell(u)}" for u in untestable]
    lines += ["", f"{JOB_MARKER}{job_id}", ""]
    return "\n".join(lines)


def strip_coderabbit_notes(body: str | None) -> str:
    """The PR body without CodeRabbit's release-notes block(s) (R27). An unterminated block runs to the end."""
    text = body or ""
    out: list[str] = []
    pos = 0
    while (m := _CR_NOTES_START.search(text, pos)) is not None:
        out.append(text[pos:m.start()])
        end = _CR_NOTES_END.search(text, m.end())
        if end is None:
            pos = len(text)
            break
        pos = end.end()
    out.append(text[pos:])
    return "".join(out)


def pr_job_ids(body: str | None) -> list[str]:
    """`gaffer-job:` ids in the PR body, ignoring CodeRabbit's block (R12, R27)."""
    return _JOB_MARKER_LINE.findall(strip_coderabbit_notes(body).replace("\r\n", "\n"))


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    created: bool


def open_pull_request(
    client: GitHubClient,
    ws: Workspace,
    *,
    title: str,
    body: str,
    scanner: SecretScanner,
) -> PullRequest:
    """Reuse this job's open PR for the branch, else open one ready for review (not draft)."""
    for pr in client.open_pulls_for_branch(ws.branch):
        ids = pr_job_ids(pr.get("body"))
        if ids == [ws.job_id]:
            return PullRequest(number=pr["number"], url=pr.get("html_url", ""), created=False)
        raise ForeignPullRequest(f"open PR #{pr.get('number')} for {ws.branch} is not this job's (markers: {ids or 'none'})")
    if pr_job_ids(body) != [ws.job_id]:
        raise ValueError("PR body must carry exactly this job's gaffer-job marker")
    scan_texts(scanner, [("title", title), ("body", body)], source="PR text").require_clean()
    pr = client.create_pull(title=title, head=ws.branch, base=ws.base_branch, body=body)
    if pr.get("draft"):
        raise GitHubError(f"PR #{pr.get('number')} was opened as a draft")
    log.info("job %s: opened PR #%s", ws.job_id, pr.get("number"))
    return PullRequest(number=pr["number"], url=pr.get("html_url", ""), created=True)


# --- round summary comment (R9, D2) ---------------------------------------------------------

ROUND_MARKER = "gaffer-round: "
_ANY_ROUND_LINE = re.compile(r"^gaffer-round: ", re.MULTILINE)


def round_marker(job_id: str, head: str, seq: int) -> str:
    if not _JOB_ID.fullmatch(job_id) or not _FULL_SHA.fullmatch(head) or int(seq) < 1:
        raise ValueError("round marker needs a job id, a full head SHA and seq >= 1")
    return f"{ROUND_MARKER}{job_id}/{head}/{int(seq)}"


def has_round_marker(body: str | None, marker: str) -> bool:
    return marker in (line.rstrip() for line in (body or "").replace("\r\n", "\n").split("\n"))


@dataclass(frozen=True)
class RoundComment:
    posted: bool
    comment_id: int | None
    url: str


def post_round_summary(
    client: GitHubClient,
    pr_number: int,
    *,
    job_id: str,
    head: str,
    seq: int,
    summary: str,
    operator_login: str,
    scanner: SecretScanner,
) -> RoundComment:
    """One summary comment per round; skipped if gaffer's own author already posted this marker (R9)."""
    marker = round_marker(job_id, head, seq)
    if _ANY_ROUND_LINE.search(summary):
        raise ValueError("the summary text must not contain its own gaffer-round line")
    for c in client.issue_comments(pr_number):
        author = ((c.get("user") or {}).get("login") or "").lower()
        if author == operator_login.lower() and has_round_marker(c.get("body"), marker):
            return RoundComment(posted=False, comment_id=c.get("id"), url=c.get("html_url", ""))
    body = f"{summary.rstrip()}\n\n{marker}\n"
    scan_texts(scanner, [("round summary", body)], source="comment").require_clean()
    c = client.create_issue_comment(pr_number, body)
    return RoundComment(posted=True, comment_id=c.get("id"), url=c.get("html_url", ""))
