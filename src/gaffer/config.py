"""gaffer.toml schema and validation, and gaffer's own Keychain lookup.

Decisions: R3 (allowed_skills, allowed_mcp), R4 (test_command, junit_path,
test_timeout), R6 (test_dirs), R7 (harness_paths), R8 (an invalid config is
exit 4, round_timeout), R11 (required checks), R14 and R29 (network
allowlist), R1, R20, R28 and R29 (deny list), R22 and R29 (runners), R27
(operator_login, coderabbit_appear_timeout).
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

# R8: refused (preflight, lock or config) exits 4.
EXIT_REFUSED = 4

ECOSYSTEMS = ("python", "node", "php")

# R22, R29: runner -> the ecosystem it belongs to; generic runs anywhere.
RUNNERS: dict[str, str | None] = {
    "pytest": "python",
    "jest": "node",
    "vitest": "node",
    "phpunit": "php",
    "generic": None,
}

# R14, amended by R29 (PHP dists come from api.github.com -> codeload).
REGISTRY_HOSTS: dict[str, tuple[str, ...]] = {
    "python": ("pypi.org", "files.pythonhosted.org"),
    "node": ("registry.npmjs.org",),
    "php": ("repo.packagist.org", "packagist.org", "api.github.com", "codeload.github.com"),
}

# R14, R29: github.com (git) is never allowed. Other GitHub hosts arrive only
# through the php ecosystem, never through extra_hosts.
NEVER_ALLOWED_HOSTS = frozenset({"github.com"})
GITHUB_DOMAINS = ("github.com", "githubusercontent.com")

# Paths no child, test run or install run may read, relative to $HOME:
# R1 and R20, plus ~/.gaffer and ~/.claude.json (R28), plus Composer's
# auth.json dirs and Herd's system-trusted CA key (R29). The vault is added
# from vault_path.
DENY_READ_HOME_PATHS = (
    ".ssh",
    ".config/gh",
    ".aws",
    "Library/Keychains",
    ".claude",
    ".codex",
    ".gaffer",
    ".claude.json",
    ".composer",
    ".config/composer",
    "Library/Application Support/Herd/config/valet",
)

# Path patterns are PurePosixPath.full_match globs over repo-relative paths.
# R7 harness defaults, plus manifests and lockfiles (R14), PHPUnit config (R29)
# and CI workflows and local actions (R30: allowed with a trailer, listed in the report).
COMMON_HARNESS_PATHS = ("**/Makefile", ".github/workflows/**", ".github/actions/**")
ECOSYSTEM_HARNESS_PATHS: dict[str, tuple[str, ...]] = {
    "python": (
        "**/conftest.py",
        "**/pytest.ini",
        "**/pyproject.toml",
        "**/setup.cfg",
        "**/setup.py",
        "**/tox.ini",
        "**/uv.lock",
        "**/requirements*.txt",
    ),
    "node": (
        "**/package.json",
        "**/package-lock.json",
        "**/npm-shrinkwrap.json",
        "**/yarn.lock",
        "**/pnpm-lock.yaml",
        "**/jest.config.*",
        "**/vitest.config.*",
        "**/vite.config.*",
    ),
    "php": (
        "**/composer.json",
        "**/composer.lock",
        "**/phpunit.xml",
        "**/phpunit.xml.dist",
    ),
}

# Design "Writer" rules: a writer commit touching these is BLOCKED: forbidden_path.
# CI workflows moved to the harness paths (R30).
FORBIDDEN_PATHS = ("**/.claude/**", "**/.codex/**", "**/.mcp.json")

DEFAULT_TEST_TIMEOUT = 15 * 60  # R4
DEFAULT_ROUND_TIMEOUT = 30 * 60  # R8
DEFAULT_CHECK_APPEAR_TIMEOUT = 10 * 60  # R11
DEFAULT_CODERABBIT_APPEAR_TIMEOUT = 10 * 60  # R27

# R30: job clones live outside ~/.gaffer, which is denied to every child (R28).
DEFAULT_JOB_ROOT = "~/gaffer-jobs"
# R28, R30: the writer's model is pinned and recorded on every launch.
DEFAULT_WRITER_MODEL = "opus"

KEYCHAIN_SERVICE = "gaffer"
GITHUB_TOKEN_ACCOUNT = "github-token"
SECURITY = "/usr/bin/security"
SECURITY_TIMEOUT = 30
SECURITY_ITEM_NOT_FOUND = 44

_TEAM_KEY = re.compile(r"^[A-Z][A-Z0-9]*$")
_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_GITHUB_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_HOST = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_BRANCH_BAD = re.compile(r"^/|[\s~^:?*\[\\]|\.\.|@\{|//|\.lock$|/$|\.$")
_SECRET_KEY = re.compile(r"token|secret|password", re.IGNORECASE)


def default_config_path(home: Path | None = None) -> Path:
    """~/.gaffer/gaffer.toml: outside the vault and inside the deny list."""
    return (home or Path.home()) / ".gaffer" / "gaffer.toml"


def deny_read_paths(home: Path, vault: Path) -> tuple[Path, ...]:
    return tuple(home / p for p in DENY_READ_HOME_PATHS) + (vault,)


def network_allowlist(ecosystems: Iterable[str], extra_hosts: Iterable[str] = ()) -> tuple[str, ...]:
    """Sandbox allowedDomains for the writer's Bash and install_command (R14, R29)."""
    ecosystems = set(ecosystems)
    hosts: list[str] = []
    for eco in ECOSYSTEMS:
        if eco in ecosystems:
            hosts.extend(REGISTRY_HOSTS[eco])
    hosts.extend(extra_hosts)
    hosts = list(dict.fromkeys(hosts))
    if blocked := NEVER_ALLOWED_HOSTS.intersection(hosts):
        raise ValueError(f"never-allowed host in network allowlist: {sorted(blocked)}")
    return tuple(hosts)


def matches_any(relpath: str, patterns: Iterable[str]) -> bool:
    path = PurePosixPath(relpath)
    return any(path.full_match(p) for p in patterns)


@dataclass(frozen=True)
class RepoConfig:
    team_key: str
    path: Path
    github: str
    base: str
    ecosystems: tuple[str, ...]
    test_command: str
    junit_path: str
    runner: str
    test_dirs: tuple[str, ...]
    harness_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    install_command: str | None
    required_checks: tuple[str, ...]
    skippable_checks: tuple[str, ...]
    allow_no_ci: bool
    extra_hosts: tuple[str, ...]
    test_timeout: int = DEFAULT_TEST_TIMEOUT
    round_timeout: int = DEFAULT_ROUND_TIMEOUT
    check_appear_timeout: int = DEFAULT_CHECK_APPEAR_TIMEOUT
    coderabbit_appear_timeout: int = DEFAULT_CODERABBIT_APPEAR_TIMEOUT

    @property
    def network_allowlist(self) -> tuple[str, ...]:
        return network_allowlist(self.ecosystems, self.extra_hosts)

    def is_harness_path(self, relpath: str) -> bool:
        return matches_any(relpath, self.harness_paths)

    def is_forbidden_path(self, relpath: str) -> bool:
        return matches_any(relpath, self.forbidden_paths)


@dataclass(frozen=True)
class Config:
    path: Path
    home: Path
    operator_login: str
    allowed_skills: tuple[str, ...]
    allowed_mcp: tuple[str, ...]
    vault_path: Path
    job_root: Path
    writer_model: str
    codex_model: str | None
    repos: Mapping[str, RepoConfig]

    @property
    def deny_read_paths(self) -> tuple[Path, ...]:
        return deny_read_paths(self.home, self.vault_path)


@dataclass(frozen=True)
class ConfigIssue:
    field: str
    problem: str
    fix: str

    def __str__(self) -> str:
        return f"{self.field}: {self.problem} Fix: {self.fix}"


class ConfigError(Exception):
    exit_code = EXIT_REFUSED

    def __init__(self, path: Path, issues: Iterable[ConfigIssue]):
        self.path = path
        self.issues = tuple(issues)
        noun = "problem" if len(self.issues) == 1 else "problems"
        lines = [f"{path} is invalid ({len(self.issues)} {noun}):"]
        lines += [f"  {issue}" for issue in self.issues]
        super().__init__("\n".join(lines))


def load_config(path: Path | None = None, *, home: Path | None = None) -> Config:
    """Read and validate gaffer.toml. Raises ConfigError listing every problem."""
    home = home or Path.home()
    path = path or default_config_path(home)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(path, [ConfigIssue(
            "(file)", "not found.", f"create {path} with operator_login, vault_path and a [repos.<TEAM>] table.",
        )]) from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(path, [ConfigIssue("(file)", f"not valid TOML: {e}.", "fix the syntax at that line.")]) from None

    issues: list[ConfigIssue] = []
    top = _Table(data, "", issues)
    operator_login = top.string("operator_login", fix='set operator_login = "<your GitHub login>" (R27).')
    if operator_login is not None and not _GITHUB_LOGIN.match(operator_login):
        top.error("operator_login", f"{operator_login!r} is not a GitHub login.", "use the login gaffer's token posts as, without @.")
    allowed_skills = top.names("allowed_skills")
    allowed_mcp = top.names("allowed_mcp")
    for name in allowed_mcp:
        if "linear" in name.lower() or name.lower().startswith(("claude_ai", "claude.ai")):
            top.error("allowed_mcp", f"{name!r} is always excluded: Linear and claude.ai connectors never reach children (R3).",
                      f"remove {name!r} from allowed_mcp.")
    vault_path = _existing_dir(top, "vault_path", home, fix='set vault_path = "<path to your Obsidian vault>"; it is denied to every child (R1, R20).')
    if vault_path is not None and path.resolve().is_relative_to(vault_path.resolve()):
        top.error("(file)", f"gaffer.toml is inside the vault ({vault_path}).", f"move it outside the vault, e.g. {default_config_path(home)}.")
    job_root = _job_root(top, home, vault_path)
    writer_model = top.string("writer_model", required=False, fix='set writer_model to a Claude model alias or ID, e.g. "opus" (R30).')
    if writer_model is not None and not _NAME.match(writer_model):
        top.error("writer_model", f"{writer_model!r} is not a model name.", 'use an alias like "opus" or a full model ID (R30).')
    codex_model = top.string("codex_model", required=False, fix='set codex_model to a Codex model name, or remove it to use the CLI default (R30).')
    if codex_model is not None and not _NAME.match(codex_model):
        top.error("codex_model", f"{codex_model!r} is not a model name.", "use a Codex model name, or remove it (R30).")

    repos: dict[str, RepoConfig] = {}
    raw_repos = top.take("repos")
    if raw_repos is None:
        top.error("repos", "missing.", "add at least one [repos.<TEAM>] table keyed by Linear team key, e.g. [repos.ENG].")
    elif not isinstance(raw_repos, dict) or not raw_repos:
        top.error("repos", "must be a table with at least one repo.", "add a [repos.<TEAM>] table, e.g. [repos.ENG].")
    else:
        for team_key, raw in raw_repos.items():
            if not _TEAM_KEY.match(team_key):
                top.error(f"repos.{team_key}", "is not a Linear team key.", "use the issue-key prefix in capitals, e.g. ENG for ENG-123.")
                continue
            if not isinstance(raw, dict):
                top.error(f"repos.{team_key}", "must be a table.", f"write it as [repos.{team_key}].")
                continue
            repo = _parse_repo(team_key, raw, home, issues)
            if repo is not None:
                repos[team_key] = repo
    top.finish()

    if issues:
        raise ConfigError(path, issues)
    assert operator_login is not None and vault_path is not None and job_root is not None
    return Config(
        path=path,
        home=home,
        operator_login=operator_login,
        allowed_skills=allowed_skills,
        allowed_mcp=allowed_mcp,
        vault_path=vault_path,
        job_root=job_root,
        writer_model=writer_model or DEFAULT_WRITER_MODEL,
        codex_model=codex_model,
        repos=repos,
    )


def _parse_repo(team_key: str, raw: dict[str, Any], home: Path, issues: list[ConfigIssue]) -> RepoConfig | None:
    t = _Table(raw, f"repos.{team_key}", issues)
    before = len(issues)

    path = _existing_dir(t, "path", home, fix='set path = "<absolute path to your local checkout>".')
    if path is not None and not (path / ".git").exists():
        t.error("path", f"{path} is not a git checkout.", "point path at the repository root (the directory holding .git).")

    github = t.string("github", fix='set github = "<owner>/<repo>".')
    if github is not None and not _GITHUB_REPO.match(github):
        t.error("github", f"{github!r} is not owner/repo.", 'write it as "owner/repo", without a URL.')

    base = t.string("base", fix='set base = "<base branch>", e.g. "main".')
    if base is not None and (base.startswith("-") or _BRANCH_BAD.search(base)):
        t.error("base", f"{base!r} is not a valid branch name.", 'use the plain branch name, e.g. "main".')

    ecosystems = t.str_list("ecosystems", fix=f'set ecosystems to one or more of {_quoted(ECOSYSTEMS)}, e.g. ["python"].')
    if ecosystems is not None:
        if not ecosystems:
            t.error("ecosystems", "is empty.", f"list at least one of {_quoted(ECOSYSTEMS)}.")
        for eco in ecosystems:
            if eco not in ECOSYSTEMS:
                t.error("ecosystems", f"{eco!r} is not supported.", f"use only {_quoted(ECOSYSTEMS)}.")

    test_command = t.string("test_command", fix='set test_command to the command that runs the tests and writes JUnit XML (R4).')
    junit_path = t.string("junit_path", fix='set junit_path to where test_command writes JUnit XML, relative to the checkout (R4).')
    if junit_path is not None and not _is_relative_inside(junit_path):
        t.error("junit_path", f"{junit_path!r} must be relative to the checkout, with no '..'.", 'use a path like "junit.xml".')

    runner = t.string("runner", fix=f"set runner to one of {_quoted(RUNNERS)} (R22).")
    if runner is not None:
        if runner not in RUNNERS:
            t.error("runner", f"{runner!r} is not supported.", f"set runner to one of {_quoted(RUNNERS)}; use \"generic\" for others (R22).")
        elif (eco := RUNNERS[runner]) is not None and ecosystems is not None and eco not in ecosystems:
            t.error("runner", f"{runner!r} needs the {eco!r} ecosystem.", f'add "{eco}" to ecosystems, or pick a runner for {_quoted(ecosystems)}.')

    test_dirs = t.str_list("test_dirs", fix='set test_dirs to the directories tester files may go in, e.g. ["tests"] (R6).')
    if test_dirs is not None:
        if not test_dirs:
            t.error("test_dirs", "is empty.", 'list at least one test directory, e.g. ["tests"] (R6).')
        for d in test_dirs:
            if not _is_relative_inside(d) or PurePosixPath(d) == PurePosixPath("."):
                t.error("test_dirs", f"{d!r} must be a subdirectory of the checkout, with no '..'.", 'use a path like "tests" (R6).')

    harness_extra = _patterns(t, "harness_paths")
    forbidden_extra = _patterns(t, "forbidden_paths")
    eco_list = [e for e in ECOSYSTEMS if ecosystems and e in ecosystems]
    harness_paths = _dedupe([*COMMON_HARNESS_PATHS, *(p for e in eco_list for p in ECOSYSTEM_HARNESS_PATHS[e]), *harness_extra])
    forbidden_paths = _dedupe([*FORBIDDEN_PATHS, *forbidden_extra])

    install_command = t.string("install_command", required=False, fix="set install_command to the dependency install command, or remove it.")

    required_checks = t.str_list("required_checks", default=(), fix='set required_checks to the CI check names that must pass, e.g. ["test"] (R11).')
    skippable_checks = t.str_list("skippable_checks", default=(), fix="list only check names from required_checks (R11).")
    allow_no_ci = t.boolean("allow_no_ci", default=False)
    if required_checks is not None and allow_no_ci is not None:
        if not required_checks and not allow_no_ci:
            t.error("required_checks", "is empty, so READY would have no CI evidence (R11).",
                    "list the required CI check names, or set allow_no_ci = true to accept READY stamped NO CI EVIDENCE.")
        if required_checks and allow_no_ci:
            t.error("allow_no_ci", "is true but required_checks is not empty, so every READY would be stamped NO CI EVIDENCE.",
                    "remove allow_no_ci, or empty required_checks.")
        if skippable_checks:
            for name in skippable_checks:
                if name not in required_checks:
                    t.error("skippable_checks", f"{name!r} is not in required_checks.", f"add {name!r} to required_checks or remove it here.")

    extra_hosts = _hosts(t, "extra_hosts")

    timeouts = {
        key: t.seconds(key, default=default)
        for key, default in (
            ("test_timeout", DEFAULT_TEST_TIMEOUT),
            ("round_timeout", DEFAULT_ROUND_TIMEOUT),
            ("check_appear_timeout", DEFAULT_CHECK_APPEAR_TIMEOUT),
            ("coderabbit_appear_timeout", DEFAULT_CODERABBIT_APPEAR_TIMEOUT),
        )
    }
    t.finish()

    if len(issues) > before:
        return None
    return RepoConfig(
        team_key=team_key,
        path=path,
        github=github,
        base=base,
        ecosystems=tuple(eco_list),
        test_command=test_command,
        junit_path=junit_path,
        runner=runner,
        test_dirs=tuple(test_dirs),
        harness_paths=harness_paths,
        forbidden_paths=forbidden_paths,
        install_command=install_command,
        required_checks=tuple(required_checks),
        skippable_checks=tuple(skippable_checks),
        allow_no_ci=allow_no_ci,
        extra_hosts=extra_hosts,
        **timeouts,
    )


_REQUIRED = object()


class _Table:
    """One TOML table being validated: takes known keys, records issues, flags the rest."""

    def __init__(self, data: dict[str, Any], where: str, issues: list[ConfigIssue]):
        self._data = dict(data)
        self._where = where
        self._issues = issues
        self._seen: list[str] = []

    def name(self, key: str) -> str:
        return f"{self._where}.{key}" if self._where else key

    def error(self, key: str, problem: str, fix: str) -> None:
        field = key if key.startswith("(") or key.startswith("repos.") else self.name(key)
        self._issues.append(ConfigIssue(field, problem, fix))

    def take(self, key: str) -> Any:
        self._seen.append(key)
        return self._data.pop(key, None)

    def _get(self, key: str, default: Any, kind: type | tuple[type, ...], kind_name: str, fix: str) -> Any:
        if key not in self._data:
            self._seen.append(key)
            if default is _REQUIRED:
                self.error(key, "missing.", fix)
                return None
            return default
        value = self.take(key)
        # bool is an int subclass; never accept it where a number is wanted.
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            self.error(key, f"must be {kind_name}, got {type(value).__name__} {value!r}.", fix)
            return None
        return value

    def string(self, key: str, *, fix: str, required: bool = True) -> str | None:
        value = self._get(key, _REQUIRED if required else None, str, "a string", fix)
        if isinstance(value, str) and not value.strip():
            self.error(key, "is empty.", fix)
            return None
        return value

    def boolean(self, key: str, *, default: bool) -> bool | None:
        return self._get(key, default, bool, "true or false", f"set {key} = true or {key} = false.")

    def seconds(self, key: str, *, default: int) -> int | None:
        fix = f"set {key} to a whole number of seconds (default {default})."
        value = self._get(key, default, int, "a whole number of seconds", fix)
        if value is not None and value <= 0:
            self.error(key, f"must be positive, got {value}.", fix)
            return None
        return value

    def str_list(self, key: str, *, fix: str, default: Any = _REQUIRED) -> tuple[str, ...] | None:
        value = self._get(key, default, (list, tuple), "a list of strings", fix)
        if value is None:
            return None
        bad = [v for v in value if not isinstance(v, str) or not v.strip()]
        if bad:
            self.error(key, f"must contain only non-empty strings, got {bad!r}.", fix)
            return None
        return tuple(value)

    def names(self, key: str) -> tuple[str, ...]:
        values = self.str_list(key, default=(), fix=f'set {key} = ["name", ...] (R3).') or ()
        for v in values:
            if not _NAME.match(v):
                self.error(key, f"{v!r} is not a valid name.", "use letters, digits, '_', '.', ':' and '-' only.")
        return values

    def finish(self) -> None:
        for key in self._data:
            if key in self._seen:
                continue
            if _SECRET_KEY.search(key):
                fix = f"remove {key}; gaffer reads its GitHub token from the macOS Keychain, never from gaffer.toml."
            else:
                fix = f"remove it or correct the spelling; known keys here: {', '.join(sorted(set(self._seen)))}."
            self.error(key, "is not a known setting.", fix)


def _existing_dir(t: _Table, key: str, home: Path, *, fix: str) -> Path | None:
    raw = t.string(key, fix=fix)
    if raw is None:
        return None
    path = Path(home / raw[2:] if raw == "~" or raw.startswith("~/") else raw)
    if not path.is_absolute():
        t.error(key, f"{raw!r} is not an absolute path.", "use an absolute path or one starting with ~/.")
        return None
    if not path.is_dir():
        t.error(key, f"{path} does not exist or is not a directory.", fix)
        return None
    return path


def _job_root(t: _Table, home: Path, vault: Path | None) -> Path | None:
    """R30: job clones must be readable by the sandbox, so never under a denied path."""
    raw = t.string("job_root", required=False, fix=f'set job_root to an absolute path outside ~/.gaffer, e.g. "{DEFAULT_JOB_ROOT}" (R30).')
    if raw is None:
        if any(i.field == "job_root" for i in t._issues):
            return None
        raw = DEFAULT_JOB_ROOT
    path = Path(home / raw[2:] if raw == "~" or raw.startswith("~/") else raw)
    if not path.is_absolute():
        t.error("job_root", f"{raw!r} is not an absolute path.", "use an absolute path or one starting with ~/.")
        return None
    denied = deny_read_paths(home, vault) if vault is not None else deny_read_paths(home, home / ".gaffer")
    for d in denied:
        if path == d or path.is_relative_to(d) or d.is_relative_to(path):
            t.error("job_root", f"{path} overlaps the denied path {d}, so the sandbox couldn't read its own job (R28, R30).",
                    f'move job_root outside every denied path, e.g. "{DEFAULT_JOB_ROOT}".')
            return None
    return path


def _patterns(t: _Table, key: str) -> tuple[str, ...]:
    values = t.str_list(key, default=(), fix=f'set {key} to repo-relative globs, e.g. ["scripts/test.sh", "ci/**"].') or ()
    for v in values:
        if not _is_relative_inside(v):
            t.error(key, f"{v!r} must be relative to the checkout, with no '..'.", 'use a repo-relative glob, e.g. "ci/**".')
    return values


def _hosts(t: _Table, key: str) -> tuple[str, ...]:
    values = t.str_list(key, default=(), fix=f'set {key} = ["registry.example.com"] or remove it (R14).') or ()
    hosts: list[str] = []
    for raw in values:
        host = raw.strip().lower().rstrip(".")
        bare = host.removeprefix("*.")
        if not _HOST.match(host):
            t.error(key, f"{raw!r} is not a hostname.", 'list bare hostnames, e.g. "registry.example.com", with no scheme, port or path.')
        elif host in NEVER_ALLOWED_HOSTS:
            t.error(key, f"{raw!r} is never allowed (R14, R29).", f"remove {raw!r}; gaffer pushes and fetches from GitHub itself.")
        elif any(bare == d or bare.endswith("." + d) or d.endswith("." + bare) for d in GITHUB_DOMAINS):
            t.error(key, f"{raw!r} is a GitHub host; GitHub hosts come only from the php ecosystem (R29).",
                    f"remove {raw!r}; a PHP repo gets api.github.com and codeload.github.com from ecosystems.")
        else:
            hosts.append(host)
    return _dedupe(hosts)


def _is_relative_inside(raw: str) -> bool:
    p = PurePosixPath(raw)
    return not p.is_absolute() and "\\" not in raw and ".." not in p.parts and not raw.startswith("~")


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _quoted(values: Iterable[str]) -> str:
    return ", ".join(f'"{v}"' for v in values)


# --- Keychain ---------------------------------------------------------------


class KeychainError(Exception):
    exit_code = EXIT_REFUSED


class Secret:
    """A credential that redacts itself everywhere except reveal().

    It isn't a str, bytes or PathLike, so subprocess refuses it as an env
    value (TypeError) instead of handing it to a child.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('<redacted>')"

    def __str__(self) -> str:
        return "<redacted>"

    def __format__(self, spec: str) -> str:
        return "<redacted>"

    def __reduce__(self):
        raise TypeError("Secret cannot be pickled")


class Keychain(Protocol):
    def find_generic_password(self, service: str, account: str) -> str | None: ...


def _run_security(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=SECURITY_TIMEOUT, check=False)


class MacKeychain:
    """The login Keychain via /usr/bin/security."""

    def __init__(self, run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None):
        self._run = run

    def find_generic_password(self, service: str, account: str) -> str | None:
        run = self._run or _run_security
        argv = [SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"]
        try:
            result = run(argv)
        except subprocess.TimeoutExpired:
            raise KeychainError(
                f"The Keychain did not answer within {SECURITY_TIMEOUT} s; it may be locked or showing an access prompt. "
                f"Fix: unlock the login Keychain and choose Always Allow for {service!r}."
            ) from None
        if result.returncode == SECURITY_ITEM_NOT_FOUND:
            return None
        if result.returncode != 0:
            # The output is deliberately not included.
            raise KeychainError(f"security find-generic-password exited {result.returncode} for service {service!r}, account {account!r}.")
        return result.stdout.removesuffix("\n")


def github_token(keychain: Keychain | None = None) -> Secret:
    """gaffer's own GitHub token. Never logged and never in a child's env."""
    keychain = keychain or MacKeychain()
    value = keychain.find_generic_password(KEYCHAIN_SERVICE, GITHUB_TOKEN_ACCOUNT)
    add = f"security add-generic-password -U -s {KEYCHAIN_SERVICE} -a {GITHUB_TOKEN_ACCOUNT} -w"
    if value is None:
        raise KeychainError(
            f"No GitHub token in the Keychain (service {KEYCHAIN_SERVICE!r}, account {GITHUB_TOKEN_ACCOUNT!r}). "
            f"Fix: run `{add}` and paste the token at the prompt."
        )
    if not value or any(c.isspace() for c in value):
        raise KeychainError(
            f"The Keychain item (service {KEYCHAIN_SERVICE!r}, account {GITHUB_TOKEN_ACCOUNT!r}) is empty or contains whitespace. "
            f"Fix: re-run `{add}` and paste only the token."
        )
    return Secret(value)
