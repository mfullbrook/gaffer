"""The only way gaffer starts a child: preflight, environment, sandboxes, launch, results.

Decisions: R1, R14, R20, R28, R29 (sandboxes and deny lists), R2 (positive
login check, never --bare, CLAUDE_CODE_SIMPLE refused), R3 and R28 (gaffer
config dirs, plugins blocked, init-event check, MCP allowlist), R9 (pre-assigned
session IDs, resume from the job clone, process groups and leases), R10
(writer result: done | blocked(question), dispositions), R5 and R22 (tester
criterion map), R15 (gitleaks present), R30 (job root, writer_model,
codex_model, MCP definitions).

Proven flag sets: docs/spike-results.md "Final working flag sets" and "3b: PHP".

Layout of a job (T7 owns creating it; this module only adds the scratch dirs):

    <job_root>/<job>/                 job dir: the sandbox write root (R21)
    <job_root>/<job>/<clone>          job clone: cwd of every writer launch and resume (R9, R28)
    <job_root>/<job>/tmp              TMPDIR for children (srt itself keeps the system TMPDIR, R29)
    <job_root>/<job>/tmp/home         HOME for Codex exec children (spike flag set)
    <job_root>/<job>/composer-home    COMPOSER_HOME and its cache (R29)
    <state_dir>                       gaffer-private: settings, MCP config, schemas, logs.
                                      Must sit under a denied path (e.g. ~/.gaffer/jobs/<job>)
                                      so no child can read or edit its own sandbox settings.
"""

from __future__ import annotations

import json
import os
import pwd
import queue
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time
import tomllib
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gaffer.config import EXIT_REFUSED, Config, RepoConfig

# --- Fixed locations and pins --------------------------------------------------

# R3: gaffer's own config dirs, each logged in once to the subscription.
CLAUDE_DIR = ".gaffer/claude"
CODEX_DIR = ".gaffer/codex"
# R30 (T5's call): full MCP server definitions live in ~/.gaffer/mcp.json, in the
# `{"mcpServers": {name: definition}}` shape claude's --mcp-config reads. Only the
# entries named in gaffer.toml `allowed_mcp` are passed to a child. The file sits
# inside ~/.gaffer, which every child is denied (R28), so a child can neither read
# server credentials in it nor add a server.
MCP_CONFIG = ".gaffer/mcp.json"
# R28, R29: sandbox-runtime pinned at 0.0.79, installed outside the repo.
SRT_DIR = ".gaffer/tools/sandbox-runtime"
SRT_BIN = SRT_DIR + "/node_modules/.bin/srt"
SRT_PACKAGE_JSON = SRT_DIR + "/node_modules/@anthropic-ai/sandbox-runtime/package.json"
SRT_VERSION = "0.0.79"
# R29: Laravel Herd's php and composer (the path has a space in it).
HERD_BIN = "Library/Application Support/Herd/bin"

# The launcher PATH from the spike (env.sh CLEAN_PATH): home dirs first.
CLEAN_PATH_HOME_DIRS = (".local/bin", ".bun/bin")
CLEAN_PATH_SYSTEM_DIRS = ("/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")

CLAUDE_MANAGED_DIR = Path("/Library/Application Support/ClaudeCode")
CODEX_MANAGED_DIR = Path("/etc/codex")

# R2: the positive login check.
CLAUDE_AUTH_METHOD = "claude.ai"
CLAUDE_API_PROVIDER = "firstParty"
CODEX_LOGIN_OK = "Logged in using ChatGPT"

# --- Limits (no gaffer.toml field exists for these; see docs/t5-children-report.md) ---

PROBE_TIMEOUT = 60
WRITER_TIMEOUT = 2 * 60 * 60
WRITER_MAX_TURNS = 80
TESTER_TIMEOUT = 20 * 60
REVIEWER_TIMEOUT = 20 * 60
INSTALL_TIMEOUT = 15 * 60
# R9: before ROUND gaffer waits up to 2 min for the writer's process group, then SIGTERM.
WRITER_GROUP_GRACE = 120.0
KILL_GRACE = 10.0
MAX_LOG_BYTES = 64 * 1024 * 1024
OUTPUT_TAIL_BYTES = 64 * 1024

# --- Claude writer flag set (spike "Final working flag sets") ---

WRITER_TOOLS = "Bash,Read,Edit,Write,Skill"
WRITER_PERMISSION_MODE = "acceptEdits"
CLAUDE_WRITER_ENV = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "BASH_DEFAULT_TIMEOUT_MS": "600000",
    "BASH_MAX_TIMEOUT_MS": "600000",
}
# R3: Linear and claude.ai connector tools are always disallowed (a_lockdown_settings.json).
LINEAR_DENY_RULES = ("mcp__claude_ai_Linear", "mcp__linear", "mcp__plugin_linear_linear")
# R28: built-in plugins seen in every init event on 2.1.304 (a_claude_init.txt). The
# init check catches any plugin not listed here; plugin_keys_from_init() names it.
KNOWN_PLUGIN_KEYS = (
    "cc-plugin-agents-md@builtin",
    "cc-plugin-telemetry@builtin",
    "cc-plugin-plugin-authoring@builtin",
    "cowork-plugin-management@synced",
)

# --- Codex flag set (spike/codex_child_args.sh) ---

CODEX_DISABLED_FEATURES = (
    "multi_agent", "apps", "plugins", "browser_use", "computer_use",
    "hooks", "image_generation", "in_app_browser", "remote_plugin", "goals",
)
CODEX_RO_PROFILE = "gaffer_ro"      # tester and reviewer (R20, R28)
CODEX_TEST_PROFILE = "gaffer_test"  # gaffer's test_command (R4, R20, R28)

_LIMIT_TEXT = re.compile(r"usage limit|rate limit|limit reached|limit exceeded", re.IGNORECASE)
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


# --- Errors --------------------------------------------------------------------


class ChildError(Exception):
    """A child could not be started safely. Refused launches exit 4 (R8)."""

    exit_code = EXIT_REFUSED


class PreflightError(ChildError):
    def __init__(self, problems: Iterable[str]):
        self.problems = tuple(problems)
        noun = "problem" if len(self.problems) == 1 else "problems"
        lines = [f"preflight refused ({len(self.problems)} {noun}):"] + [f"  {p}" for p in self.problems]
        super().__init__("\n".join(lines))


class InitCheckFailed(ChildError):
    """R28: the child's init event showed a plugin, an unlisted MCP server or a non-subscription
    key source, so gaffer killed it before it acted."""

    def __init__(self, problems: Sequence[str], init: Mapping[str, Any] | None, lease: ChildLease | None):
        self.problems = tuple(problems)
        self.init = dict(init) if init else None
        self.lease = lease
        super().__init__("child killed at its init event: " + "; ".join(self.problems))


class ResultError(ValueError):
    """A structured result is missing or malformed. Callers map it to a BLOCKED reason
    (tester_failed / tester_invalid, writer_no_result, reviewer_failed)."""

    def __init__(self, problems: Iterable[str]):
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


class _KillChild(Exception):
    def __init__(self, reason: str, problems: Sequence[str] = ()):
        self.reason = reason
        self.problems = tuple(problems) or (reason,)
        super().__init__(reason)


# --- Paths ---------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    """Machine-level locations. Tests build one over a temporary home and fake CLIs."""

    home: Path
    search_path: str
    system_tmpdir: str
    claude_managed_dir: Path = CLAUDE_MANAGED_DIR
    codex_managed_dir: Path = CODEX_MANAGED_DIR

    @classmethod
    def for_home(cls, home: Path | None = None, *, search_path: str | None = None,
                 environ: Mapping[str, str] | None = None, **kw: Any) -> Paths:
        home = home or Path.home()
        environ = os.environ if environ is None else environ
        if search_path is None:
            search_path = ":".join([*(str(home / d) for d in CLEAN_PATH_HOME_DIRS), *CLEAN_PATH_SYSTEM_DIRS])
        # R29: srt binds a Unix socket in TMPDIR, so it keeps the short system one.
        system_tmpdir = environ.get("TMPDIR") or "/tmp"
        return cls(home=home, search_path=search_path, system_tmpdir=system_tmpdir, **kw)

    @property
    def claude_config_dir(self) -> Path:
        return self.home / CLAUDE_DIR

    @property
    def codex_home(self) -> Path:
        return self.home / CODEX_DIR

    @property
    def mcp_config(self) -> Path:
        return self.home / MCP_CONFIG

    @property
    def srt(self) -> Path:
        return self.home / SRT_BIN

    @property
    def srt_package_json(self) -> Path:
        return self.home / SRT_PACKAGE_JSON

    @property
    def herd_bin(self) -> Path:
        return self.home / HERD_BIN

    def which(self, name: str) -> str | None:
        return shutil.which(name, path=self.search_path)

    def require(self, name: str) -> str:
        found = self.which(name)
        if found is None:
            raise ChildError(f"{name} not found on gaffer's child PATH ({self.search_path}).")
        return found


@dataclass(frozen=True)
class JobDirs:
    job_dir: Path
    clone: Path
    state_dir: Path

    @property
    def tmp(self) -> Path:
        return self.job_dir / "tmp"

    @property
    def codex_child_home(self) -> Path:
        return self.tmp / "home"

    @property
    def composer_home(self) -> Path:
        return self.job_dir / "composer-home"

    @property
    def composer_cache(self) -> Path:
        return self.composer_home / "cache"

    @property
    def logs(self) -> Path:
        return self.state_dir / "logs"

    def validate(self, cfg: Config) -> None:
        problems: list[str] = []
        job, clone, state = (p.resolve() for p in (self.job_dir, self.clone, self.state_dir))
        denied = [d.resolve() for d in cfg.deny_read_paths]
        if not clone.is_relative_to(job) or clone == job:
            problems.append(f"job clone {clone} is not inside the job dir {job} (R21).")
        if not job.is_relative_to(cfg.job_root.resolve()):
            problems.append(f"job dir {job} is not under job_root {cfg.job_root} (R30).")
        if any(job == d or job.is_relative_to(d) or d.is_relative_to(job) for d in denied):
            problems.append(f"job dir {job} overlaps a denied path (R28, R30).")
        if not any(state.is_relative_to(d) for d in denied):
            problems.append(f"state dir {state} must be under a denied path such as ~/.gaffer, so children can't read or edit it.")
        if problems:
            raise ChildError(" ".join(problems))

    def ensure(self) -> None:
        for d in (self.tmp, self.codex_child_home, self.composer_cache, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)


def new_session_id() -> str:
    """R9: the writer's session ID, assigned (and recorded by the caller) before launch."""
    return str(uuid.uuid4())


# --- Environment allowlist -----------------------------------------------------

BASE_ENV_KEYS = ("HOME", "PATH", "USER", "TMPDIR", "LANG", "TERM", "CLAUDE_CONFIG_DIR", "CODEX_HOME")
COMPOSER_ENV_KEYS = ("COMPOSER_HOME", "COMPOSER_CACHE_DIR", "COMPOSER_NO_INTERACTION")
ALLOWED_ENV_KEYS = frozenset(BASE_ENV_KEYS + COMPOSER_ENV_KEYS + tuple(CLAUDE_WRITER_ENV))


def forbidden_env_key(key: str) -> bool:
    """R2: keys that could move a child off the subscription. Children never get them
    (the env is rebuilt from scratch), and preflight refuses to run while they're set."""
    return (
        key == "CLAUDE_CODE_SIMPLE"
        or key.startswith(("ANTHROPIC_", "OPENAI_", "AZURE_OPENAI_"))
        or key.endswith("_BASE_URL")
        or key in {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                   "CODEX_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"}
    )


def child_env(paths: Paths, *, jobs: JobDirs | None = None, php: bool = False, role: str = "probe",
              environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a child's whole environment: the `env -i` allowlist, nothing inherited.

    role: "probe" (version and auth checks), "writer", "codex" (tester/reviewer),
    "test" (codex sandbox) or "install" (srt).
    """
    environ = os.environ if environ is None else environ
    path = paths.search_path
    if php and role in ("writer", "test", "install"):
        path = f"{paths.herd_bin}:{path}"  # R29
    env = {
        "HOME": str(paths.home),
        "PATH": path,
        "USER": environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name,
        "TMPDIR": str(jobs.tmp) if jobs else paths.system_tmpdir,
        "LANG": environ.get("LANG") or "en_US.UTF-8",
        "TERM": environ.get("TERM") or "xterm-256color",
        "CLAUDE_CONFIG_DIR": str(paths.claude_config_dir),
        "CODEX_HOME": str(paths.codex_home),
    }
    if role == "writer":
        env.update(CLAUDE_WRITER_ENV)
    if role == "codex":
        if jobs is None:
            raise ChildError("Codex children need a job dir.")
        env["HOME"] = str(jobs.codex_child_home)
    if role == "install":
        env["TMPDIR"] = paths.system_tmpdir  # R29: srt keeps the short system TMPDIR
        env["TERM"] = "dumb"
    if php and jobs is not None and role in ("writer", "test", "install"):
        env["COMPOSER_HOME"] = str(jobs.composer_home)
        env["COMPOSER_CACHE_DIR"] = str(jobs.composer_cache)
        env["COMPOSER_NO_INTERACTION"] = "1"
    _check_env(env)
    return env


def _check_env(env: Mapping[str, str]) -> None:
    extra = set(env) - ALLOWED_ENV_KEYS
    bad = [k for k in env if forbidden_env_key(k)]
    if extra or bad:
        raise ChildError(f"child environment outside the allowlist: {sorted(extra | set(bad))}")


# --- Claude settings (R1, R14, R28, R29) ---------------------------------------


def read_deny_rules(deny: Iterable[Path]) -> list[str]:
    """File-tool Read rules. `//abs` is an absolute path in permission syntax. Each entry
    gets an exact-path rule (single files such as ~/.claude.json, R29) and a glob."""
    rules: list[str] = []
    for p in deny:
        rules += [f"Read(/{p})", f"Read(/{p}/**)"]
    return rules


def writer_settings(cfg: Config, repo: RepoConfig, jobs: JobDirs, *, plugin_keys: Iterable[str] = ()) -> dict[str, Any]:
    """The writer's --settings file."""
    deny = [str(p) for p in cfg.deny_read_paths]
    plugins = dict.fromkeys([*KNOWN_PLUGIN_KEYS, *plugin_keys], False)
    return {
        "permissions": {
            "deny": ["WebFetch", "WebSearch", *LINEAR_DENY_RULES, *read_deny_rules(cfg.deny_read_paths)],
            "additionalDirectories": [],
        },
        "disableAllHooks": True,
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {"allowWrite": [str(jobs.job_dir)], "denyRead": deny},
            "network": {"allowedDomains": list(repo.network_allowlist)},
        },
        # R28: plugins blocked (built-ins included, since plugins can carry hooks); bundled skills off.
        "enabledPlugins": plugins,
        "disableBundledSkills": True,
    }


def srt_settings(cfg: Config, jobs: JobDirs, hosts: Iterable[str]) -> dict[str, Any]:
    """sandbox-runtime settings for install_command (R28, R29; spike p_srt_settings.sh)."""
    return {
        "network": {"allowedDomains": list(hosts), "deniedDomains": []},
        "filesystem": {"denyRead": [str(p) for p in cfg.deny_read_paths], "allowWrite": [str(jobs.job_dir)], "denyWrite": []},
    }


def load_mcp_definitions(cfg: Config, paths: Paths) -> dict[str, Any]:
    """The `--mcp-config` content: only the servers named in allowed_mcp (R3, R30)."""
    if not cfg.allowed_mcp:
        return {"mcpServers": {}}
    try:
        data = json.loads(paths.mcp_config.read_text())
        servers = data["mcpServers"]
        if not isinstance(servers, dict):
            raise TypeError
    except FileNotFoundError:
        raise ChildError(f"allowed_mcp lists {list(cfg.allowed_mcp)} but {paths.mcp_config} does not exist. "
                         'Fix: write {"mcpServers": {"<name>": {...}}} there.') from None
    except (ValueError, KeyError, TypeError):
        raise ChildError(f'{paths.mcp_config} must be JSON of the form {{"mcpServers": {{"<name>": {{...}}}}}}.') from None
    missing = [n for n in cfg.allowed_mcp if n not in servers]
    if missing:
        raise ChildError(f"{paths.mcp_config} has no definition for allowed_mcp {missing}.")
    return {"mcpServers": {n: servers[n] for n in cfg.allowed_mcp}}


# --- Codex permission profiles (R20, R28) ----------------------------------------


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def codex_profile_args(name: str, deny: Iterable[Path], *, write: Iterable[Path] = ()) -> list[str]:
    """`-c` overrides defining a named permission profile: read everything, write only
    `write`, deny every credential path, no network (spike/codex_profiles.sh)."""
    entries = ['":root"="read"']
    entries += [f'{_toml_str(str(p))}="write"' for p in write]
    entries += [f'{_toml_str(str(p))}="deny"' for p in deny]
    return [
        "-c", f'default_permissions="{name}"',
        "-c", f"permissions.{name}.filesystem={{{','.join(entries)}}}",
        "-c", f"permissions.{name}.network.enabled=false",
    ]


def codex_exec_argv(codex: str, cfg: Config, jobs: JobDirs, *, schema_path: Path, result_path: Path, prompt: str) -> list[str]:
    _check_prompt(prompt)
    argv = [codex, "exec", "--json", "--ignore-rules", *codex_profile_args(CODEX_RO_PROFILE, cfg.deny_read_paths),
            "-c", "project_doc_max_bytes=0", "-c", "skills.include_instructions=false", "-c", 'web_search="disabled"']
    for feature in CODEX_DISABLED_FEATURES:
        argv += ["--disable", feature]
    if cfg.codex_model:
        argv += ["-m", cfg.codex_model]  # R30
    argv += ["--output-schema", str(schema_path), "-o", str(result_path), "-C", str(jobs.clone), prompt]
    return argv


def _check_prompt(prompt: str) -> None:
    if not prompt.strip() or prompt.lstrip().startswith("-"):
        raise ChildError("a prompt must be non-empty and must not start with '-'.")


# --- Processes, groups and leases (R9) -------------------------------------------


@dataclass(frozen=True)
class ChildLease:
    """What the store records to decide later whether a child is still alive (R9)."""

    role: str
    pid: int
    pgid: int
    start_time: str
    cwd: str
    session_id: str | None
    started_at: float  # wall clock, for the report


def process_start_time(pid: int) -> str | None:
    """The process's start time as `ps` prints it. None if no such process."""
    try:
        out = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = out.stdout.strip()
    return value or None


def lease_alive(lease: ChildLease) -> bool:
    """R9: alive means the PID exists with the same start time."""
    start = process_start_time(lease.pid)
    return start is not None and start == lease.start_time


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_group(pgid: int, timeout: float) -> bool:
    """Wait for every process in the group to exit. True if it emptied in time."""
    deadline = time.monotonic() + timeout
    while group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def terminate_group(pgid: int, grace: float = KILL_GRACE, proc: subprocess.Popen | None = None) -> bool:
    """SIGTERM the group, SIGKILL after `grace`. Returns True if anything was signalled."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if proc is not None:
            proc.poll()  # reap our own child so its zombie doesn't keep the group alive
        if not group_alive(pgid):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    return True


@dataclass(frozen=True)
class RunOutcome:
    lease: ChildLease
    exit_code: int | None
    timed_out: bool
    killed: str | None
    duration: float
    stdout_tail: str
    stderr_tail: str
    log_path: Path | None
    group_terminated: bool


class _Tail:
    def __init__(self, limit: int):
        self._chunks: deque[str] = deque()
        self._size = 0
        self._limit = limit

    def add(self, s: str) -> None:
        self._chunks.append(s)
        self._size += len(s)
        while self._size > self._limit and len(self._chunks) > 1:
            self._size -= len(self._chunks.popleft())

    def text(self) -> str:
        return "".join(self._chunks)[-self._limit:]


_EOF = object()


def _run_child(argv: Sequence[str], *, role: str, env: Mapping[str, str], cwd: Path, timeout: float,
               log_path: Path | None = None, on_line: Callable[[str], None] | None = None,
               on_spawn: Callable[[ChildLease], None] | None = None, session_id: str | None = None,
               group_grace: float = 0.0) -> RunOutcome:
    """Run one child in its own process group with stdin /dev/null, stream its stdout
    line by line, enforce the timeout, and clean up the group.

    on_line may raise _KillChild to stop the child at once (R28 init check).
    group_grace: after the main process exits, how long other group members may run
    before SIGTERM (R9: 2 min for the writer; 0 for gaffer's own runs).
    """
    if "--bare" in argv:
        raise ChildError("--bare is never passed: it forces API-key billing (R2).")
    _check_env(env)
    started = time.monotonic()
    proc = subprocess.Popen(list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = proc.pid
    lease = ChildLease(role=role, pid=proc.pid, pgid=pgid, start_time=process_start_time(proc.pid) or "",
                       cwd=str(cwd), session_id=session_id, started_at=time.time())

    lines: queue.Queue[Any] = queue.Queue()
    err_tail = _Tail(OUTPUT_TAIL_BYTES)
    out_tail = _Tail(OUTPUT_TAIL_BYTES)

    def read_out() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            lines.put(raw.decode("utf-8", "replace"))
        lines.put(_EOF)

    def read_err() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            err_tail.add(raw.decode("utf-8", "replace"))

    threads = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for t in threads:
        t.start()

    timed_out = False
    killed: str | None = None
    kill_error: _KillChild | None = None
    log = log_path.open("w", encoding="utf-8") if log_path else None
    logged = 0
    exited_at: float | None = None
    try:
        if on_spawn is not None:
            on_spawn(lease)
        deadline = started + timeout
        while True:
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                killed = "timeout"
                terminate_group(pgid, KILL_GRACE, proc)
                break
            try:
                item = lines.get(timeout=min(0.2, deadline - now))
            except queue.Empty:
                if proc.poll() is not None:
                    # The main process is gone; a grandchild may still hold stdout open.
                    exited_at = exited_at or time.monotonic()
                    if time.monotonic() - exited_at > 1.0:
                        break
                continue
            if item is _EOF:
                break
            out_tail.add(item)
            if log is not None and logged < MAX_LOG_BYTES:
                log.write(item)
                logged += len(item)
            if on_line is not None:
                try:
                    on_line(item)
                except _KillChild as k:
                    killed = k.reason
                    kill_error = k
                    terminate_group(pgid, KILL_GRACE, proc)
                    break
        if not timed_out and killed is None:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                killed = "timeout"
                terminate_group(pgid, KILL_GRACE, proc)
    finally:
        if log is not None:
            log.close()
        if proc.poll() is None:
            terminate_group(pgid, KILL_GRACE, proc)
            proc.wait()

    group_terminated = False
    if group_alive(pgid):
        if group_grace <= 0 or not wait_group(pgid, group_grace):
            group_terminated = terminate_group(pgid, KILL_GRACE)
    for t in threads:
        t.join(timeout=2)
    outcome = RunOutcome(lease=lease, exit_code=proc.returncode if not timed_out else None, timed_out=timed_out,
                         killed=killed, duration=time.monotonic() - started, stdout_tail=out_tail.text(),
                         stderr_tail=err_tail.text(), log_path=log_path, group_terminated=group_terminated)
    if kill_error is not None:
        raise _KilledAtInit(kill_error, outcome)
    return outcome


class _KilledAtInit(Exception):
    def __init__(self, k: _KillChild, outcome: RunOutcome):
        self.kill = k
        self.outcome = outcome


# --- Limits and usage --------------------------------------------------------------


@dataclass(frozen=True)
class LimitEvent:
    """A usage-limit signal: the runner ends the job BLOCKED: usage_limit (design, Limits)."""

    provider: str
    detail: str
    resets_at: int | None = None


# --- Preflight (R2, R3, R15, R28, R29) --------------------------------------------


@dataclass(frozen=True)
class PreflightReport:
    """Recorded on each job (R2): CLI versions and login status, identifiers redacted."""

    claude_version: str
    codex_version: str
    gitleaks_version: str
    srt_version: str | None
    claude_auth: Mapping[str, Any]
    codex_login: str
    mcp_servers: tuple[str, ...]  # allowlisted servers run outside the sandbox (R3); the report lists them


_REDACT_KEYS = re.compile(r"email|org|account|user", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("<redacted>" if _REDACT_KEYS.search(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _EMAIL.sub("<redacted-email>", value)
    return value


def _probe(argv: Sequence[str], env: Mapping[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    if "--bare" in argv:
        raise ChildError("--bare is never passed (R2).")
    return subprocess.run(list(argv), env=dict(env), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout, start_new_session=True, check=False)


def env_problems(environ: Mapping[str, str]) -> list[str]:
    problems = []
    for key in sorted(environ):
        if key == "CLAUDE_CODE_SIMPLE":
            problems.append("CLAUDE_CODE_SIMPLE is set; it switches Claude to API-key billing (R2). Fix: unset it.")
        elif forbidden_env_key(key) and environ[key]:
            problems.append(f"{key} is set in gaffer's environment; API keys, provider switches and base URLs are refused (R2). Fix: unset it.")
    return problems


_CLAUDE_FORBIDDEN_SETTINGS = ("apiKeyHelper", "awsCredentialExport", "awsAuthRefresh", "gcpAuthRefresh")


def claude_settings_problems(path: Path) -> list[str]:
    """R2, R3: managed or gaffer-user Claude settings must not set env, apiKeyHelper or a provider."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        return [f"{path} could not be read as a JSON object; refusing rather than guessing (R2)."]
    problems = []
    if data.get("env"):
        problems.append(f"{path} sets an env block ({sorted(data['env'])}); refused (R2, R3).")
    for key in _CLAUDE_FORBIDDEN_SETTINGS:
        if key in data:
            problems.append(f"{path} sets {key}; refused (R2, R3).")
    if str(data.get("forceLoginMethod", "")).lower() == "console":
        problems.append(f"{path} forces a Console (API) login; refused (R2).")
    return problems


_CODEX_FORBIDDEN_KEYS = frozenset({"model_provider", "model_providers", "base_url", "openai_base_url",
                                   "chatgpt_base_url", "env_key", "experimental_bearer_token", "mcp_servers"})


def codex_config_problems(path: Path) -> list[str]:
    """R2, R3: Codex config must not set a provider, a base URL or MCP servers, must not force
    API-key login, and must not trust any project (a trusted project loads its .codex config)."""
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return [f"{path} could not be read as TOML; refusing rather than guessing (R2)."]
    problems = []

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                here = f"{where}.{k}" if where else k
                if k in _CODEX_FORBIDDEN_KEYS:
                    problems.append(f"{path} sets {here}; refused (R2, R3).")
                elif k in ("preferred_auth_method", "forced_login_method") and str(v).lower() in ("apikey", "api"):
                    problems.append(f"{path} sets {here} = {v!r}; refused (R2).")
                elif k == "trust_level" and where.startswith("projects."):
                    problems.append(f"{path} trusts a project ({where}); gaffer never trusts projects (spike A4).")
                else:
                    walk(v, here)

    walk(data, "")
    return problems


def managed_settings_files(paths: Paths) -> tuple[list[Path], list[Path]]:
    claude = [paths.claude_managed_dir / "managed-settings.json"]
    d = paths.claude_managed_dir / "managed-settings.d"
    if d.is_dir():
        claude += sorted(d.glob("*.json"))
    codex = [paths.codex_managed_dir / "managed_config.toml", paths.codex_managed_dir / "requirements.toml"]
    return claude, codex


def preflight(cfg: Config, repo: RepoConfig | None = None, *, paths: Paths | None = None,
              environ: Mapping[str, str] | None = None, timeout: float = PROBE_TIMEOUT) -> PreflightReport:
    """Every check before any child starts. Collects every problem, then refuses (exit 4)."""
    paths = paths or Paths.for_home(cfg.home)
    environ = os.environ if environ is None else environ
    problems = env_problems(environ)

    for d, name in ((paths.claude_config_dir, "CLAUDE_CONFIG_DIR"), (paths.codex_home, "CODEX_HOME")):
        if not d.is_dir():
            problems.append(f"{name} dir {d} is missing. Fix: log in once into gaffer's own dir (R3).")

    claude_managed, codex_managed = managed_settings_files(paths)
    for p in [*claude_managed, paths.claude_config_dir / "settings.json", paths.claude_config_dir / "settings.local.json"]:
        problems += claude_settings_problems(p)
    for p in [*codex_managed, paths.codex_home / "config.toml"]:
        problems += codex_config_problems(p)

    try:
        load_mcp_definitions(cfg, paths)
    except ChildError as e:
        problems.append(str(e))

    env = child_env(paths, environ=environ)
    claude, codex, gitleaks = paths.which("claude"), paths.which("codex"), paths.which("gitleaks")
    versions: dict[str, str] = {}
    claude_auth: dict[str, Any] = {}
    codex_login = ""

    def version(name: str, binary: str | None, argv: list[str]) -> None:
        if binary is None:
            fix = "brew install gitleaks (R15)" if name == "gitleaks" else f"install {name} on gaffer's PATH"
            problems.append(f"{name} not found on {paths.search_path}. Fix: {fix}.")
            return
        try:
            out = _probe([binary, *argv], env, timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            problems.append(f"`{name} {' '.join(argv)}` failed: {e}.")
            return
        text = (out.stdout or out.stderr).strip()
        if out.returncode != 0 or not text:
            problems.append(f"`{name} {' '.join(argv)}` exited {out.returncode}.")
            return
        versions[name] = text.splitlines()[0]

    version("claude", claude, ["--version"])
    version("codex", codex, ["--version"])
    version("gitleaks", gitleaks, ["version"])

    if claude is not None:
        try:
            out = _probe([claude, "auth", "status", "--json"], env, timeout)
            status = json.loads(out.stdout)
            if not isinstance(status, dict):
                raise ValueError
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            problems.append(f"`claude auth status --json` gave no JSON object ({type(e).__name__}); refusing (R2).")
        else:
            claude_auth = redact(status)
            want = {"loggedIn": True, "authMethod": CLAUDE_AUTH_METHOD, "apiProvider": CLAUDE_API_PROVIDER}
            for k, v in want.items():
                if status.get(k) != v:
                    problems.append(f"claude auth status: {k} is {status.get(k)!r}, need {v!r} (R2). "
                                    f"Fix: CLAUDE_CONFIG_DIR={paths.claude_config_dir} claude auth login --claudeai.")
            cd = status.get("configDirectory")
            if cd is not None and _expand(cd, paths.home) != paths.claude_config_dir:
                problems.append(f"claude auth status reports configDirectory {cd!r}, not {paths.claude_config_dir} (R3).")

    if codex is not None:
        try:
            out = _probe([codex, "login", "status"], env, timeout)
            codex_login = (out.stdout + out.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            problems.append(f"`codex login status` failed: {e}.")
        else:
            if codex_login != CODEX_LOGIN_OK:
                problems.append(f"codex login status said {redact(codex_login)!r}, need {CODEX_LOGIN_OK!r} (R2). "
                                f"Fix: CODEX_HOME={paths.codex_home} codex login --device-auth.")

    srt_version = None
    if repo is not None and repo.install_command:
        srt_version = _srt_version(paths)
        if not paths.srt.exists():
            problems.append(f"sandbox-runtime not found at {paths.srt}; install_command needs it (R28). "
                            f"Fix: npm install --ignore-scripts --save-exact @anthropic-ai/sandbox-runtime@{SRT_VERSION} in {paths.home / SRT_DIR}.")
        elif srt_version != SRT_VERSION:
            problems.append(f"sandbox-runtime is {srt_version!r}, pinned {SRT_VERSION} (R29).")
        if paths.which("node") is None:
            problems.append("node not found on gaffer's PATH; sandbox-runtime needs it.")
    if repo is not None and "php" in repo.ecosystems:
        for tool in ("php", "composer"):
            if not (paths.herd_bin / tool).exists():
                problems.append(f"{paths.herd_bin / tool} is missing; PHP repos use Laravel Herd (R29).")

    if problems:
        raise PreflightError(problems)
    return PreflightReport(
        claude_version=versions["claude"], codex_version=versions["codex"], gitleaks_version=versions["gitleaks"],
        srt_version=srt_version, claude_auth=claude_auth, codex_login=codex_login, mcp_servers=tuple(cfg.allowed_mcp),
    )


def _expand(raw: str, home: Path) -> Path:
    return home / raw[2:] if raw.startswith("~/") else Path(raw)


def _srt_version(paths: Paths) -> str | None:
    try:
        return json.loads(paths.srt_package_json.read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        return None


# --- Claude stream-json and the init check (R28) ------------------------------------


def _plugin_key(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("source") or entry.get("name") or entry)
    return str(entry)


def _mcp_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("name"))
    return str(entry)


def plugin_keys_from_init(init: Mapping[str, Any]) -> list[str]:
    """Plugin keys for `enabledPlugins: false`, as gen_lockdown.sh built them."""
    return [_plugin_key(p) for p in init.get("plugins") or []]


def init_problems(init: Mapping[str, Any], allowed_mcp: Iterable[str]) -> list[str]:
    """R28: plugins must be [] and every MCP server must be allowlisted. Also R2: the key
    source must be "none" (it was on every subscription run; anything else is a key)."""
    problems = []
    plugins = init.get("plugins")
    if plugins is None or not isinstance(plugins, list):
        problems.append("init event has no plugins list")
    elif plugins:
        problems.append(f"plugins loaded: {plugin_keys_from_init(init)}")
    servers = init.get("mcp_servers")
    if servers is None or not isinstance(servers, list):
        problems.append("init event has no mcp_servers list")
    else:
        allowed = set(allowed_mcp)
        extra = [_mcp_name(s) for s in servers if _mcp_name(s) not in allowed]
        if extra:
            problems.append(f"MCP servers not in allowed_mcp: {extra}")
    if init.get("apiKeySource") != "none":
        problems.append(f"apiKeySource is {init.get('apiKeySource')!r}, not 'none' (R2)")
    return problems


class ClaudeStream:
    """Consumes `--output-format stream-json` lines."""

    def __init__(self, allowed_mcp: Iterable[str]):
        self.allowed_mcp = tuple(allowed_mcp)
        self.init: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None
        self.rate_limits: list[dict[str, Any]] = []
        self.limit: LimitEvent | None = None
        self.events = 0

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        self.events += 1
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            self.init = ev
            problems = init_problems(ev, self.allowed_mcp)
            if problems:
                raise _KillChild("init check failed", problems)
            return
        if self.init is None and kind != "system":
            raise _KillChild("child acted before its init event", [f"{kind!r} event before init"])
        if kind == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            self.rate_limits.append(info)
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                self.limit = LimitEvent("claude", f"rate_limit_event status {info.get('status')!r} ({info.get('rateLimitType')})",
                                        info.get("resetsAt"))
        elif kind == "result":
            self.result = ev
            text = str(ev.get("result") or "")
            if ev.get("is_error") and _LIMIT_TEXT.search(text) and self.limit is None:
                self.limit = LimitEvent("claude", text[:500])


# --- Structured results (R5, R10, R22) ---------------------------------------------

_STR = {"type": "string"}
_NSTR = {"type": ["string", "null"]}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


TESTER_SCHEMA = _obj({
    "criteria": {"type": "array", "items": _obj({"id": _STR, "text": _STR})},
    "untestable": {"type": "array", "items": _obj({"criterion_id": _STR, "reason": _STR})},
    "tests": {"type": "array", "items": _obj({
        "criterion_id": _STR, "label": {"type": "string", "enum": ["acceptance", "guard"]},
        "file": _STR, "classname": _STR, "name": _STR})},
    "expected_missing": {"type": "array", "items": _STR},
    "files": {"type": "array", "items": _obj({"path": _STR, "content": _STR})},
})
WRITER_SCHEMA = _obj({"status": {"type": "string", "enum": ["done", "blocked"]}, "summary": _STR, "question": _NSTR})
ADDRESS_SCHEMA = _obj({
    **WRITER_SCHEMA["properties"],
    "dispositions": {"type": "array", "items": _obj({
        "item_id": _STR, "disposition": {"type": "string", "enum": ["fixed", "declined"]},
        "commit_sha": _NSTR, "reason": _STR})},
})
REVIEW_SCHEMA = _obj({
    "summary": _STR,
    "findings": {"type": "array", "items": _obj({
        "id": _STR, "severity": {"type": "string", "enum": ["blocker", "major", "minor", "nit"]},
        "path": _NSTR, "line": {"type": ["integer", "null"]}, "title": _STR, "body": _STR})},
    "harness_judgements": {"type": "array", "items": _obj({
        "path": _STR, "verdict": {"type": "string", "enum": ["justified", "unjustified"]}, "reason": _STR})},
})


@dataclass(frozen=True)
class TestIdentity:
    """A test's identity as JUnit reports it: file + classname + name (R22)."""

    __test__ = False  # not a pytest class
    file: str
    classname: str
    name: str


@dataclass(frozen=True)
class TesterTest:
    __test__ = False
    criterion_id: str
    label: str  # "acceptance" or "guard" (R5)
    identity: TestIdentity


@dataclass(frozen=True)
class TesterResult:
    __test__ = False
    criteria: tuple[tuple[str, str], ...]          # (id, text)
    untestable: tuple[tuple[str, str], ...]        # (criterion id, reason): carried into the report as unverified
    tests: tuple[TesterTest, ...]
    expected_missing: tuple[str, ...]
    files: tuple[tuple[str, str], ...]             # (repo-relative path, full contents); T7 validates paths (R6)

    @property
    def testable_criteria(self) -> tuple[str, ...]:
        """Empty means BLOCKED: no_testable_criteria."""
        skip = {c for c, _ in self.untestable}
        return tuple(c for c, _ in self.criteria if c not in skip)

    @property
    def identities(self) -> tuple[TestIdentity, ...]:
        return tuple(t.identity for t in self.tests)


def _require(data: Any, key: str, kind: type | tuple[type, ...], problems: list[str], where: str) -> Any:
    if not isinstance(data, dict) or key not in data:
        problems.append(f"{where}{key} missing")
        return None
    value = data[key]
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        problems.append(f"{where}{key} has the wrong type")
        return None
    return value


def _strings(data: Any, keys: Sequence[str], problems: list[str], where: str, *, nonempty: bool = True) -> list[str] | None:
    out = []
    for k in keys:
        v = _require(data, k, str, problems, where)
        if v is None:
            return None
        if nonempty and not v.strip():
            problems.append(f"{where}{k} is empty")
            return None
        out.append(v)
    return out


def parse_tester_result(data: Any) -> TesterResult:
    """Validate the tester's structured output. Raise ResultError (one retry, then
    BLOCKED: tester_invalid / tester_failed). Path rules (R6) are workspace's."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise ResultError(["tester result is not a JSON object"])
    criteria: list[tuple[str, str]] = []
    for i, c in enumerate(_require(data, "criteria", list, problems, "") or []):
        s = _strings(c, ("id", "text"), problems, f"criteria[{i}].")
        if s:
            criteria.append((s[0], s[1]))
    ids = [c for c, _ in criteria]
    if len(set(ids)) != len(ids):
        problems.append("criterion ids are not unique")
    if not criteria and "criteria" in data:
        problems.append("no criteria")
    untestable: list[tuple[str, str]] = []
    for i, u in enumerate(_require(data, "untestable", list, problems, "") or []):
        s = _strings(u, ("criterion_id", "reason"), problems, f"untestable[{i}].")
        if s:
            if s[0] not in ids:
                problems.append(f"untestable[{i}] names unknown criterion {s[0]!r}")
            untestable.append((s[0], s[1]))
    tests: list[TesterTest] = []
    for i, t in enumerate(_require(data, "tests", list, problems, "") or []):
        s = _strings(t, ("criterion_id", "label", "file", "name"), problems, f"tests[{i}].")
        classname = _require(t, "classname", str, problems, f"tests[{i}].")  # "" is valid (pytest collection errors)
        if not s or classname is None:
            continue
        cid, label, file, name = s
        if cid not in ids:
            problems.append(f"tests[{i}] names unknown criterion {cid!r}")
        if label not in ("acceptance", "guard"):
            problems.append(f"tests[{i}].label {label!r} is not acceptance or guard")
        tests.append(TesterTest(cid, label, TestIdentity(file, classname, name)))
    seen = [t.identity for t in tests]
    if len(set(seen)) != len(seen):
        problems.append("test identities are not unique")
    expected_missing = []
    for i, sym in enumerate(_require(data, "expected_missing", list, problems, "") or []):
        if not isinstance(sym, str) or not sym.strip():
            problems.append(f"expected_missing[{i}] is not a symbol name")
        else:
            expected_missing.append(sym)
    files: list[tuple[str, str]] = []
    for i, f in enumerate(_require(data, "files", list, problems, "") or []):
        path = _strings(f, ("path",), problems, f"files[{i}].")
        content = _require(f, "content", str, problems, f"files[{i}].")
        if path and content is not None:
            files.append((path[0], content))
    paths = [p for p, _ in files]
    if len(set(paths)) != len(paths):
        problems.append("file paths are not unique")
    # R5: at least one acceptance test per testable criterion.
    untestable_ids = {c for c, _ in untestable}
    for cid in ids:
        if cid not in untestable_ids and not any(t.criterion_id == cid and t.label == "acceptance" for t in tests):
            problems.append(f"criterion {cid!r} has no acceptance test and isn't marked untestable")
    if problems:
        raise ResultError(problems)
    return TesterResult(tuple(criteria), tuple(untestable), tuple(tests), tuple(expected_missing), tuple(files))


@dataclass(frozen=True)
class Disposition:
    item_id: str
    disposition: str  # "fixed" or "declined"
    commit_sha: str | None
    reason: str


@dataclass(frozen=True)
class WriterResult:
    status: str  # "done" or "blocked" (R10)
    summary: str
    question: str | None
    dispositions: tuple[Disposition, ...] = ()


def parse_writer_result(data: Any, *, item_ids: Sequence[str] | None = None) -> WriterResult:
    """R10: done | blocked(question); for address runs, one disposition per packet item,
    fixed with a commit SHA or declined with a reason. Raises ResultError, which the runner
    reports as BLOCKED: writer_no_result. Whether each SHA is a new commit on the branch is
    checked by the caller against git (R10)."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise ResultError(["writer result is missing or not a JSON object"])
    status = _require(data, "status", str, problems, "")
    summary = _require(data, "summary", str, problems, "")
    question = data.get("question")
    if question is not None and not isinstance(question, str):
        problems.append("question has the wrong type")
        question = None
    if status not in ("done", "blocked"):
        problems.append(f"status {status!r} is not done or blocked")
    if status == "blocked" and not (question and question.strip()):
        problems.append("blocked without a question")
    if status == "done":
        question = None
    dispositions: list[Disposition] = []
    if item_ids is not None:
        raw = _require(data, "dispositions", list, problems, "") or []
        for i, d in enumerate(raw):
            s = _strings(d, ("item_id", "disposition"), problems, f"dispositions[{i}].")
            reason = _require(d, "reason", str, problems, f"dispositions[{i}].")
            sha = d.get("commit_sha") if isinstance(d, dict) else None
            if not s or reason is None:
                continue
            item_id, verdict = s
            if verdict == "fixed":
                if not isinstance(sha, str) or not _SHA.match(sha):
                    problems.append(f"dispositions[{i}] is fixed without a commit SHA")
            elif verdict == "declined":
                if not reason.strip():
                    problems.append(f"dispositions[{i}] is declined without a reason")
                sha = None
            else:
                problems.append(f"dispositions[{i}].disposition {verdict!r} is not fixed or declined")
            dispositions.append(Disposition(item_id, verdict, sha if isinstance(sha, str) else None, reason))
        if status == "done":
            got = [d.item_id for d in dispositions]
            missing = [i for i in item_ids if i not in got]
            unknown = [i for i in got if i not in item_ids]
            dupes = sorted({i for i in got if got.count(i) > 1})
            if missing:
                problems.append(f"no disposition for items {missing}")
            if unknown:
                problems.append(f"dispositions for unknown items {unknown}")
            if dupes:
                problems.append(f"more than one disposition for items {dupes}")
    if problems:
        raise ResultError(problems)
    return WriterResult(status, summary or "", question, tuple(dispositions))


@dataclass(frozen=True)
class ReviewFinding:
    id: str
    severity: str
    path: str | None
    line: int | None
    title: str
    body: str


@dataclass(frozen=True)
class HarnessJudgement:
    path: str
    verdict: str  # "justified" or "unjustified" (R7)
    reason: str


@dataclass(frozen=True)
class ReviewResult:
    summary: str
    findings: tuple[ReviewFinding, ...]
    harness_judgements: tuple[HarnessJudgement, ...]


def parse_review_result(data: Any) -> ReviewResult:
    problems: list[str] = []
    if not isinstance(data, dict):
        raise ResultError(["review result is not a JSON object"])
    summary = _require(data, "summary", str, problems, "")
    findings = []
    for i, f in enumerate(_require(data, "findings", list, problems, "") or []):
        s = _strings(f, ("id", "severity", "title", "body"), problems, f"findings[{i}].")
        if not s:
            continue
        if s[1] not in ("blocker", "major", "minor", "nit"):
            problems.append(f"findings[{i}].severity {s[1]!r} is unknown")
        path, line = f.get("path"), f.get("line")
        findings.append(ReviewFinding(s[0], s[1], path if isinstance(path, str) else None,
                                      line if isinstance(line, int) and not isinstance(line, bool) else None, s[2], s[3]))
    judgements = []
    for i, j in enumerate(_require(data, "harness_judgements", list, problems, "") or []):
        s = _strings(j, ("path", "verdict", "reason"), problems, f"harness_judgements[{i}].")
        if not s:
            continue
        if s[1] not in ("justified", "unjustified"):
            problems.append(f"harness_judgements[{i}].verdict {s[1]!r} is unknown")
        judgements.append(HarnessJudgement(*s))
    if problems:
        raise ResultError(problems)
    return ReviewResult(summary or "", tuple(findings), tuple(judgements))


# --- Claude writer: launch and resume (R9, R10, R28, R30) ----------------------------


@dataclass(frozen=True)
class WriterRun:
    session_id: str
    lease: ChildLease
    exit_code: int | None
    timed_out: bool
    duration: float
    init: Mapping[str, Any] | None
    result_event: Mapping[str, Any] | None
    result: WriterResult | None
    result_error: str | None        # set when result is None: BLOCKED: writer_no_result (or a timeout)
    limit: LimitEvent | None        # BLOCKED: usage_limit
    model: str
    claude_version: str | None
    skills: tuple[str, ...]         # account-synced skills are accepted and listed in the report (R28)
    usage: Mapping[str, Any]
    num_turns: int | None
    permission_denials: tuple[Any, ...]
    log_path: Path | None
    group_terminated: bool          # R9: other group members outlived the grace period and were SIGTERMed
    stderr_tail: str


def _write_private(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)
    return path


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S") + f"-{secrets.token_hex(3)}"


def writer_argv(claude: str, cfg: Config, *, prompt: str, settings_path: Path, mcp_config: str,
                session_id: str, resume: bool, schema: Mapping[str, Any], max_turns: int) -> list[str]:
    _check_prompt(prompt)
    return [
        claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
        "--json-schema", json.dumps(schema, separators=(",", ":")),
        "--settings", str(settings_path), "--setting-sources", "user",
        "--mcp-config", mcp_config, "--strict-mcp-config",
        "--tools", WRITER_TOOLS, "--permission-mode", WRITER_PERMISSION_MODE,
        "--model", cfg.writer_model, "--max-turns", str(max_turns),
        "--resume" if resume else "--session-id", session_id,
    ]


def _validate_session_id(session_id: str) -> str:
    try:
        return str(uuid.UUID(session_id))
    except (ValueError, TypeError, AttributeError):
        raise ChildError(f"session id {session_id!r} is not a UUID (R9).") from None


def launch_writer(cfg: Config, repo: RepoConfig, jobs: JobDirs, prompt: str, *, session_id: str,
                  item_ids: Sequence[str] | None = None, paths: Paths | None = None,
                  on_spawn: Callable[[ChildLease], None] | None = None, timeout: float = WRITER_TIMEOUT,
                  max_turns: int = WRITER_MAX_TURNS, plugin_keys: Iterable[str] = (),
                  group_grace: float = WRITER_GROUP_GRACE) -> WriterRun:
    """First writer turn with a pre-assigned --session-id (R9). The caller records the
    session ID before calling, and the lease from on_spawn as soon as it arrives.

    item_ids: packet item ids for an address.md run (dispositions required); None for implement.md.
    Raises InitCheckFailed if the child was killed at its init event (R28): add
    plugin_keys_from_init(e.init) to plugin_keys and relaunch with a new session ID.
    """
    return _claude_writer(cfg, repo, jobs, prompt, session_id=session_id, resume=False, item_ids=item_ids,
                          paths=paths, on_spawn=on_spawn, timeout=timeout, max_turns=max_turns,
                          plugin_keys=plugin_keys, group_grace=group_grace)


def resume_writer(cfg: Config, repo: RepoConfig, jobs: JobDirs, session_id: str, prompt: str, *,
                  item_ids: Sequence[str] | None = None, paths: Paths | None = None,
                  on_spawn: Callable[[ChildLease], None] | None = None, timeout: float = WRITER_TIMEOUT,
                  max_turns: int = WRITER_MAX_TURNS, plugin_keys: Iterable[str] = (),
                  group_grace: float = WRITER_GROUP_GRACE) -> WriterRun:
    """Resume the same session. Always runs from the job clone: sessions are keyed by cwd (R9, R28)."""
    return _claude_writer(cfg, repo, jobs, prompt, session_id=session_id, resume=True, item_ids=item_ids,
                          paths=paths, on_spawn=on_spawn, timeout=timeout, max_turns=max_turns,
                          plugin_keys=plugin_keys, group_grace=group_grace)


# R10 nudge and the design's "continue" resume after a dead writer.
NUDGE_PROMPT = ("gaffer found uncommitted changes in the job clone. Commit them or discard them "
                "(`git status` must be clean), then return the structured result again.")
CONTINUE_PROMPT = ("gaffer was interrupted while you were working. Continue the same task from where you "
                   "left off, commit your work, and return the structured result.")


def _claude_writer(cfg: Config, repo: RepoConfig, jobs: JobDirs, prompt: str, *, session_id: str, resume: bool,
                   item_ids: Sequence[str] | None, paths: Paths | None, on_spawn: Callable[[ChildLease], None] | None,
                   timeout: float, max_turns: int, plugin_keys: Iterable[str], group_grace: float) -> WriterRun:
    paths = paths or Paths.for_home(cfg.home)
    session_id = _validate_session_id(session_id)
    jobs.validate(cfg)
    jobs.ensure()
    claude = paths.require("claude")
    stamp = _stamp()
    settings_path = _write_private(jobs.state_dir / "writer-settings.json",
                                   json.dumps(writer_settings(cfg, repo, jobs, plugin_keys=plugin_keys), indent=2))
    mcp = load_mcp_definitions(cfg, paths)
    if mcp["mcpServers"]:
        mcp_arg = str(_write_private(jobs.state_dir / "mcp.json", json.dumps(mcp)))
    else:
        mcp_arg = json.dumps(mcp, separators=(",", ":"))  # '{"mcpServers":{}}', as proven
    schema = ADDRESS_SCHEMA if item_ids is not None else WRITER_SCHEMA
    argv = writer_argv(claude, cfg, prompt=prompt, settings_path=settings_path, mcp_config=mcp_arg,
                       session_id=session_id, resume=resume, schema=schema, max_turns=max_turns)
    env = child_env(paths, jobs=jobs, php="php" in repo.ecosystems, role="writer")
    stream = ClaudeStream(cfg.allowed_mcp)
    log_path = jobs.logs / f"writer-{stamp}.jsonl"
    try:
        out = _run_child(argv, role="writer", env=env, cwd=jobs.clone, timeout=timeout, log_path=log_path,
                         on_line=stream.feed, on_spawn=on_spawn, session_id=session_id, group_grace=group_grace)
    except _KilledAtInit as k:
        raise InitCheckFailed(k.kill.problems, stream.init, k.outcome.lease) from None

    result: WriterResult | None = None
    error: str | None = None
    ev = stream.result
    if out.timed_out:
        error = f"writer timed out after {timeout:.0f} s"
    elif stream.init is None:
        error = "writer exited without an init event"
    elif ev is None:
        error = f"writer exited {out.exit_code} without a result event"
    elif ev.get("session_id") not in (None, session_id):
        error = f"result session_id {ev.get('session_id')!r} is not the assigned {session_id}"
    elif ev.get("is_error") or ev.get("subtype") != "success":
        error = f"writer result {ev.get('subtype')!r}: {str(ev.get('result') or '')[:500]}"
    else:
        try:
            result = parse_writer_result(ev.get("structured_output"), item_ids=item_ids)
        except ResultError as e:
            error = f"malformed writer result: {e}"
    init = stream.init or {}
    return WriterRun(
        session_id=session_id, lease=out.lease, exit_code=out.exit_code, timed_out=out.timed_out, duration=out.duration,
        init=stream.init, result_event=ev, result=result, result_error=error, limit=stream.limit,
        model=cfg.writer_model, claude_version=init.get("claude_code_version"),
        skills=tuple(str(s) for s in init.get("skills") or []), usage=(ev or {}).get("usage") or {},
        num_turns=(ev or {}).get("num_turns"), permission_denials=tuple((ev or {}).get("permission_denials") or []),
        log_path=log_path, group_terminated=out.group_terminated, stderr_tail=out.stderr_tail,
    )


# --- Codex tester and reviewer (R5, R20, R22, R28, R30) -------------------------------


@dataclass(frozen=True)
class CodexRun:
    role: str
    lease: ChildLease
    exit_code: int | None
    timed_out: bool
    duration: float
    thread_id: str | None
    data: Any                     # the parsed -o JSON, or None
    result_error: str | None      # one retry, then BLOCKED: tester_failed / reviewer_failed
    limit: LimitEvent | None
    usage: Mapping[str, Any]
    rate_limits: Mapping[str, Any] | None
    model: str | None
    log_path: Path | None
    stderr_tail: str


@dataclass(frozen=True)
class TesterRun:
    run: CodexRun
    result: TesterResult | None
    result_error: str | None


@dataclass(frozen=True)
class ReviewRun:
    run: CodexRun
    result: ReviewResult | None
    result_error: str | None


class CodexStream:
    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.usage: dict[str, Any] = {}
        self.errors: list[str] = []

    def feed(self, line: str) -> None:
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        kind = ev.get("type")
        if kind == "thread.started":
            self.thread_id = ev.get("thread_id")
        elif kind == "turn.completed":
            for k, v in (ev.get("usage") or {}).items():
                if isinstance(v, int):
                    self.usage[k] = self.usage.get(k, 0) + v
        elif kind == "error":
            self.errors.append(str(ev.get("message") or ev))
        elif kind == "turn.failed":
            self.errors.append(str((ev.get("error") or {}).get("message") or ev))


def codex_rate_limits(codex_home: Path, thread_id: str | None) -> dict[str, Any] | None:
    """The last `token_count` rate_limits in the session rollout (Experiment 1)."""
    if not thread_id:
        return None
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return None
    last = None
    for rollout in sessions.rglob(f"rollout-*{thread_id}.jsonl"):
        for line in rollout.read_text(errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            payload = ev.get("payload") if isinstance(ev, dict) else None
            if isinstance(payload, dict) and payload.get("type") == "token_count" and payload.get("rate_limits"):
                last = payload["rate_limits"]
    return last


def codex_limit_event(rate_limits: Mapping[str, Any] | None, errors: Sequence[str]) -> LimitEvent | None:
    if rate_limits:
        reached = rate_limits.get("rate_limit_reached_type")
        windows = [w for w in (rate_limits.get("primary"), rate_limits.get("secondary")) if isinstance(w, dict)]
        full = [w for w in windows if (w.get("used_percent") or 0) >= 100]
        if reached or full:
            resets = max((w.get("resets_at") or 0 for w in (full or windows)), default=0) or None
            return LimitEvent("codex", f"rate limit reached ({reached or 'used_percent >= 100'})", resets)
    for e in errors:
        if _LIMIT_TEXT.search(e):
            return LimitEvent("codex", e[:500])
    return None


def _codex_exec(role: str, cfg: Config, jobs: JobDirs, prompt: str, schema: Mapping[str, Any], *,
                paths: Paths | None, on_spawn: Callable[[ChildLease], None] | None, timeout: float) -> CodexRun:
    paths = paths or Paths.for_home(cfg.home)
    jobs.validate(cfg)
    jobs.ensure()
    codex = paths.require("codex")
    stamp = _stamp()
    schema_path = _write_private(jobs.state_dir / f"{role}-schema.json", json.dumps(schema))
    result_path = jobs.state_dir / f"{role}-{stamp}.result.json"
    argv = codex_exec_argv(codex, cfg, jobs, schema_path=schema_path, result_path=result_path, prompt=prompt)
    env = child_env(paths, jobs=jobs, role="codex")
    stream = CodexStream()
    log_path = jobs.logs / f"{role}-{stamp}.jsonl"
    out = _run_child(argv, role=role, env=env, cwd=jobs.clone, timeout=timeout, log_path=log_path,
                     on_line=stream.feed, on_spawn=on_spawn)
    rate_limits = codex_rate_limits(paths.codex_home, stream.thread_id)
    limit = codex_limit_event(rate_limits, stream.errors)
    data, error = None, None
    if out.timed_out:
        error = f"{role} timed out after {timeout:.0f} s"
    else:
        try:
            data = json.loads(result_path.read_text())
        except FileNotFoundError:
            error = f"{role} exited {out.exit_code} without a result" + (f": {stream.errors[-1][:300]}" if stream.errors else "")
        except ValueError:
            error = f"{role} result is not JSON"
    return CodexRun(role=role, lease=out.lease, exit_code=out.exit_code, timed_out=out.timed_out, duration=out.duration,
                    thread_id=stream.thread_id, data=data, result_error=error, limit=limit, usage=stream.usage,
                    rate_limits=rate_limits, model=cfg.codex_model, log_path=log_path, stderr_tail=out.stderr_tail)


def run_tester(cfg: Config, jobs: JobDirs, prompt: str, *, paths: Paths | None = None,
               on_spawn: Callable[[ChildLease], None] | None = None, timeout: float = TESTER_TIMEOUT) -> TesterRun:
    """Codex tester, read-only under gaffer_ro, in the unchanged job clone (R5, R20, R22)."""
    run = _codex_exec("tester", cfg, jobs, prompt, TESTER_SCHEMA, paths=paths, on_spawn=on_spawn, timeout=timeout)
    if run.result_error:
        return TesterRun(run, None, run.result_error)
    try:
        return TesterRun(run, parse_tester_result(run.data), None)
    except ResultError as e:
        return TesterRun(run, None, f"invalid tester result: {e}")


def run_reviewer(cfg: Config, jobs: JobDirs, prompt: str, *, paths: Paths | None = None,
                 on_spawn: Callable[[ChildLease], None] | None = None, timeout: float = REVIEWER_TIMEOUT) -> ReviewRun:
    """Codex reviewer, read-only under gaffer_ro, once per head (design: Codex reviewer)."""
    run = _codex_exec("reviewer", cfg, jobs, prompt, REVIEW_SCHEMA, paths=paths, on_spawn=on_spawn, timeout=timeout)
    if run.result_error:
        return ReviewRun(run, None, run.result_error)
    try:
        return ReviewRun(run, parse_review_result(run.data), None)
    except ResultError as e:
        return ReviewRun(run, None, f"invalid review result: {e}")


# --- gaffer's own runs: test_command and install_command -----------------------------


@dataclass(frozen=True)
class CommandRun:
    command: str
    cwd: Path
    lease: ChildLease
    exit_code: int | None
    timed_out: bool
    duration: float
    output_tail: str   # stdout tail; gitleaks-scan before it enters any packet (R24)
    stderr_tail: str
    log_path: Path | None


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def sandboxed_test_argv(codex: str, cfg: Config, jobs: JobDirs, cwd: Path, command: str) -> list[str]:
    return [codex, "sandbox", *codex_profile_args(CODEX_TEST_PROFILE, cfg.deny_read_paths, write=[jobs.job_dir]),
            "-P", CODEX_TEST_PROFILE, "-C", str(cwd), "--", "/bin/bash", "-c", command]



def run_test_command(cfg: Config, repo: RepoConfig, jobs: JobDirs, cwd: Path, *, command: str | None = None,
                     paths: Paths | None = None, on_spawn: Callable[[ChildLease], None] | None = None,
                     timeout: float | None = None) -> CommandRun:
    """test_command (or one per-file adapter command, R29) under the Codex gaffer_test profile:
    credentials denied, writes only in the job dir, no network (R4, R20, R28). The JUnit file
    is read by reviews.py; the exit code is never trusted on its own (R4)."""
    paths = paths or Paths.for_home(cfg.home)
    jobs.validate(cfg)
    jobs.ensure()
    if not _inside(cwd, jobs.job_dir):
        raise ChildError(f"test cwd {cwd} is outside the job dir {jobs.job_dir}.")
    command = command or repo.test_command
    argv = sandboxed_test_argv(paths.require("codex"), cfg, jobs, cwd, command)
    env = child_env(paths, jobs=jobs, php="php" in repo.ecosystems, role="test")
    log_path = jobs.logs / f"test-{_stamp()}.log"
    out = _run_child(argv, role="test", env=env, cwd=cwd, timeout=timeout or repo.test_timeout, log_path=log_path,
                     on_spawn=on_spawn)
    return CommandRun(command, cwd, out.lease, out.exit_code, out.timed_out, out.duration, out.stdout_tail,
                      out.stderr_tail, log_path)



def install_argv(srt: Path, settings_path: Path, jobs: JobDirs, checkout: Path, install_command: str) -> list[str]:
    # R29: srt keeps the system TMPDIR; the wrapped command moves TMPDIR into the job.
    # No /dev/fd process substitution inside srt (it's denied there).
    script = f"export TMPDIR={shlex.quote(str(jobs.tmp))}; cd {shlex.quote(str(checkout))} && {install_command}"
    return [str(srt), "--settings", str(settings_path), "-c", script]


def run_install_command(cfg: Config, repo: RepoConfig, jobs: JobDirs, checkout: Path, *, hosts: Iterable[str] | None = None,
                        paths: Paths | None = None, on_spawn: Callable[[ChildLease], None] | None = None,
                        timeout: float = INSTALL_TIMEOUT) -> CommandRun:
    """install_command under sandbox-runtime 0.0.79 (R14, R28, R29). hosts defaults to the repo's
    network allowlist; pass () for an offline install from the job's warm cache (R29 PHP worktrees)."""
    paths = paths or Paths.for_home(cfg.home)
    if not repo.install_command:
        raise ChildError(f"repo {repo.team_key} has no install_command.")
    jobs.validate(cfg)
    jobs.ensure()
    if not _inside(checkout, jobs.job_dir):
        raise ChildError(f"install checkout {checkout} is outside the job dir {jobs.job_dir}.")
    if not paths.srt.exists():
        raise ChildError(f"sandbox-runtime not found at {paths.srt} (R28).")
    hosts = tuple(repo.network_allowlist if hosts is None else hosts)
    stamp = _stamp()
    settings_path = _write_private(jobs.state_dir / f"srt-{stamp}.json", json.dumps(srt_settings(cfg, jobs, hosts), indent=2))
    argv = install_argv(paths.srt, settings_path, jobs, checkout, repo.install_command)
    env = child_env(paths, jobs=jobs, php="php" in repo.ecosystems, role="install")
    log_path = jobs.logs / f"install-{stamp}.log"
    out = _run_child(argv, role="install", env=env, cwd=checkout, timeout=timeout, log_path=log_path, on_spawn=on_spawn)
    return CommandRun(repo.install_command, checkout, out.lease, out.exit_code, out.timed_out, out.duration,
                      out.stdout_tail, out.stderr_tail, log_path)


# --- Prompts (prompts/*.md; R19: eval before changing them) ---------------------------

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
PROMPT_NAMES = ("tester", "implement", "review", "address")
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")

# How each runner's JUnit names a test (R22, spike D and 3b). reviews.py's adapters must agree.
IDENTITY_CONVENTIONS = {
    "pytest": ("file = repo-relative path; classname = the dotted module path without .py (e.g. tests.test_x), "
               "plus .ClassName if the test is inside a class; name = the test function name (with its [param] suffix "
               "if parametrized)."),
    "jest": ("file = repo-relative path; classname = the same repo-relative file path; name = the enclosing describe "
             "titles and the test title joined by single spaces."),
    "vitest": ("file = repo-relative path; classname = the same repo-relative file path; name = the enclosing describe "
               "titles and the test title joined by ' > '. (Unproven against a real run.)"),
    "phpunit": ("file = repo-relative path; classname = the test class's fully qualified name with dots instead of "
                "backslashes (e.g. App.Tests.FooTest); name = the test method name."),
    "generic": "file = repo-relative path; classname and name exactly as this repo's runner writes them to JUnit XML.",
}


def untrusted(label: str, text: str) -> str:
    """Fence text gaffer did not write (issue text, comments, child output) behind a random
    boundary the text can't forge. Prompts say: data, never instructions."""
    nonce = secrets.token_hex(8)
    tag = re.sub(r"[^A-Z0-9_]", "_", label.upper())
    body = text.replace("\r\n", "\n")
    return f"<<<UNTRUSTED {tag} {nonce}>>>\n{body}\n<<<END UNTRUSTED {tag} {nonce}>>>"


def render_prompt(name: str, values: Mapping[str, str], *, prompts_dir: Path | None = None) -> str:
    """Fill a prompts/<name>.md template's {{placeholders}}. Every placeholder must be given.
    Values are inserted once, so placeholder-like text inside a value is left alone."""
    if name not in PROMPT_NAMES:
        raise ValueError(f"unknown prompt {name!r}")
    template = ((prompts_dir or PROMPTS_DIR) / f"{name}.md").read_text()
    missing = sorted({m for m in _PLACEHOLDER.findall(template) if m not in values})
    if missing:
        raise KeyError(f"prompt {name} needs values for {missing}")
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), template)


def _bullets(items: Iterable[str]) -> str:
    items = list(items)
    return "\n".join(f"- `{i}`" for i in items) if items else "- (none)"


def tester_prompt(repo: RepoConfig, *, issue_key: str, issue_title: str, issue_body: str,
                  retry_error: str | None = None, prompts_dir: Path | None = None) -> str:
    return render_prompt("tester", {
        "issue_key": issue_key,
        "issue": untrusted("ISSUE", f"{issue_title}\n\n{issue_body}"),
        "runner": repo.runner,
        "identity_convention": IDENTITY_CONVENTIONS[repo.runner],
        "test_dirs": _bullets(repo.test_dirs),
        "retry": (untrusted("PREVIOUS_ATTEMPT_ERROR", retry_error) if retry_error else "(first attempt)"),
    }, prompts_dir=prompts_dir)


def _criterion_map_text(tester: TesterResult) -> str:
    lines = []
    for cid, text in tester.criteria:
        lines.append(f"{cid}: {text}")
        for t in tester.tests:
            if t.criterion_id == cid:
                i = t.identity
                lines.append(f"  - [{t.label}] {i.file} :: {i.classname} :: {i.name}")
    for cid, reason in tester.untestable:
        lines.append(f"{cid} is untestable: {reason}")
    lines.append("expected_missing: " + (", ".join(tester.expected_missing) or "(none)"))
    return "\n".join(lines)


def implement_prompt(repo: RepoConfig, jobs: JobDirs, *, issue_key: str, issue_title: str, issue_body: str,
                     tester: TesterResult, tester_commit: str, base_sha: str, prompts_dir: Path | None = None) -> str:
    return render_prompt("implement", {
        "issue_key": issue_key,
        "issue": untrusted("ISSUE", f"{issue_title}\n\n{issue_body}"),
        "criterion_map": untrusted("CRITERION_MAP", _criterion_map_text(tester)),
        "tester_commit": tester_commit,
        "base_sha": base_sha,
        "test_command": repo.test_command,
        "test_dirs": _bullets(repo.test_dirs),
        "harness_paths": _bullets(repo.harness_paths),
        "forbidden_paths": _bullets(repo.forbidden_paths),
        "install_notes": _install_notes(repo, jobs),
        "allowed_hosts": ", ".join(repo.network_allowlist) or "(none)",
    }, prompts_dir=prompts_dir)


def _install_notes(repo: RepoConfig, jobs: JobDirs) -> str:
    notes = []
    if "python" in repo.ecosystems:
        notes.append("- Python: install with `uv` (e.g. `uv add`, `uv pip install`); plain pip fails TLS in the sandbox, "
                     "so if you must use pip add `--use-deprecated=legacy-certs`.")
    if "node" in repo.ecosystems:
        notes.append(f"- Node: pass `--cache {jobs.job_dir / '.npm-cache'}` to npm.")
    if "php" in repo.ecosystems:
        notes.append("- PHP: `composer` and `php` are on PATH (Laravel Herd); COMPOSER_HOME is already set inside the job.")
    return "\n".join(notes) or "- No dependency installs are expected."


@dataclass(frozen=True)
class PromptItem:
    """One packet item as the writer sees it (T8 builds packets; T9 converts them)."""

    id: str
    source: str        # e.g. "coderabbit", "codex", "operator", "ci", "gaffer-verify"
    location: str      # "path:line", a check name, or "" when none applies
    body: str


def review_prompt(repo: RepoConfig, *, issue_key: str, issue_title: str, issue_body: str, tester: TesterResult,
                  base_sha: str, head_sha: str, harness_changes: Sequence[tuple[str, str]],
                  prompts_dir: Path | None = None) -> str:
    """harness_changes: (path, the commit's Tester-Change-Reason) for every tester or harness file
    the writer changed (R7) and every manifest or lockfile change (R14)."""
    changes = "\n".join(f"- {p}: {r}" for p, r in harness_changes) or "(none)"
    return render_prompt("review", {
        "issue_key": issue_key,
        "issue": untrusted("ISSUE", f"{issue_title}\n\n{issue_body}"),
        "criterion_map": untrusted("CRITERION_MAP", _criterion_map_text(tester)),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "harness_changes": untrusted("HARNESS_CHANGES", changes),
        "test_dirs": _bullets(repo.test_dirs),
    }, prompts_dir=prompts_dir)


def address_prompt(repo: RepoConfig, *, head_sha: str, items: Sequence[PromptItem], cycle: int, max_cycles: int,
                   prompts_dir: Path | None = None) -> str:
    rendered = []
    for it in items:
        loc = f" at {it.location}" if it.location else ""
        rendered.append(f"### item {it.id} (from {it.source}{loc})\n{it.body}")
    return render_prompt("address", {
        "head_sha": head_sha,
        "cycle": str(cycle),
        "max_cycles": str(max_cycles),
        "item_ids": ", ".join(it.id for it in items) or "(none)",
        "packet": untrusted("PACKET", "\n\n".join(rendered) or "(no items)"),
        "test_command": repo.test_command,
        "harness_paths": _bullets(repo.harness_paths),
        "test_dirs": _bullets(repo.test_dirs),
    }, prompts_dir=prompts_dir)

