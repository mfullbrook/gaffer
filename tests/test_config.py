import logging
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from gaffer import config
from gaffer.config import ConfigError, KeychainError, Secret


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    (home / "vault").mkdir(parents=True)
    (home / "src" / "app" / ".git").mkdir(parents=True)
    (home / ".gaffer").mkdir()
    return home


def write(home: Path, body: str, *, globals_: str | None = None) -> Path:
    if globals_ is None:
        globals_ = 'operator_login = "mfullbrook"\nvault_path = "~/vault"\n'
    path = home / ".gaffer" / "gaffer.toml"
    path.write_text(globals_ + textwrap.dedent(body))
    return path


REPO = """
[repos.ENG]
path = "~/src/app"
github = "mfullbrook/app"
base = "main"
ecosystems = ["python"]
test_command = "uv run pytest --junitxml=junit.xml"
junit_path = "junit.xml"
runner = "pytest"
test_dirs = ["tests"]
required_checks = ["test"]
"""


def repo(**overrides: str) -> str:
    """REPO with keys replaced (value is TOML text) or removed (value None)."""
    lines = []
    for line in REPO.strip().splitlines():
        key = line.split(" = ")[0]
        if key in overrides:
            if overrides[key] is not None:
                lines.append(f"{key} = {overrides.pop(key)}")
            else:
                overrides.pop(key)
        else:
            lines.append(line)
    lines += [f"{k} = {v}" for k, v in overrides.items() if v is not None]
    return "\n".join(lines) + "\n"


def load(home, body=REPO, **kw):
    return config.load_config(write(home, body, **kw), home=home)


def issues(home, body=REPO, **kw) -> dict[str, str]:
    with pytest.raises(ConfigError) as exc:
        load(home, body, **kw)
    assert exc.value.exit_code == 4
    return {i.field: str(i) for i in exc.value.issues}


# --- valid config and defaults ---------------------------------------------


def test_minimal_config_loads_with_defaults(home):
    cfg = load(home)
    assert cfg.operator_login == "mfullbrook"
    assert cfg.allowed_skills == () and cfg.allowed_mcp == ()
    assert cfg.vault_path == home / "vault"
    assert cfg.job_root == home / "gaffer-jobs"
    assert (cfg.writer_model, cfg.codex_model) == ("opus", None)
    r = cfg.repos["ENG"]
    assert r.path == home / "src" / "app"
    assert (r.github, r.base, r.runner, r.junit_path) == ("mfullbrook/app", "main", "pytest", "junit.xml")
    assert r.ecosystems == ("python",)
    assert r.test_dirs == ("tests",)
    assert r.install_command is None
    assert r.required_checks == ("test",) and r.skippable_checks == () and r.allow_no_ci is False
    assert r.extra_hosts == ()
    assert (r.test_timeout, r.round_timeout, r.check_appear_timeout, r.coderabbit_appear_timeout) == (900, 1800, 600, 600)


def test_full_config_loads(home):
    globals_ = textwrap.dedent("""
        operator_login = "mfullbrook"
        vault_path = "~/vault"
        allowed_skills = ["code-loop", "gstack:review"]
        allowed_mcp = ["context7"]
        job_root = "~/work/jobs"
        writer_model = "claude-opus-5-5"
        codex_model = "gpt-6-astra"
    """)
    body = repo(
        ecosystems='["python", "node"]',
        install_command='"uv sync && npm ci"',
        skippable_checks='["test"]',
        extra_hosts='["Registry.Example.com."]',
        harness_paths='["scripts/run-tests.sh"]',
        forbidden_paths='["deploy/**"]',
        test_timeout="60",
        round_timeout="120",
        check_appear_timeout="30",
        coderabbit_appear_timeout="45",
    )
    cfg = load(home, body, globals_=globals_)
    assert cfg.allowed_skills == ("code-loop", "gstack:review")
    assert cfg.allowed_mcp == ("context7",)
    assert cfg.job_root == home / "work" / "jobs"
    assert (cfg.writer_model, cfg.codex_model) == ("claude-opus-5-5", "gpt-6-astra")
    r = cfg.repos["ENG"]
    assert r.install_command == "uv sync && npm ci"
    assert r.skippable_checks == ("test",)
    assert r.extra_hosts == ("registry.example.com",)
    assert (r.test_timeout, r.round_timeout, r.check_appear_timeout, r.coderabbit_appear_timeout) == (60, 120, 30, 45)
    assert r.is_harness_path("scripts/run-tests.sh")
    assert r.is_forbidden_path("deploy/prod.yml")


def test_multiple_repos(home):
    (home / "src" / "web" / ".git").mkdir(parents=True)
    web = repo(path='"~/src/web"', ecosystems='["node"]', runner='"jest"').replace("repos.ENG", "repos.WEB")
    cfg = load(home, REPO + web)
    assert set(cfg.repos) == {"ENG", "WEB"}


def test_default_config_path_is_inside_gaffer_dir(home):
    assert config.default_config_path(home) == home / ".gaffer" / "gaffer.toml"
    write(home, REPO)
    assert config.load_config(home=home).repos["ENG"].team_key == "ENG"


# --- network allowlist (R14, R29) ------------------------------------------


@pytest.mark.parametrize(
    ("ecosystems", "expected"),
    [
        (["python"], ("pypi.org", "files.pythonhosted.org")),
        (["node"], ("registry.npmjs.org",)),
        (["php"], ("repo.packagist.org", "packagist.org", "api.github.com", "codeload.github.com")),
        (
            ["php", "python", "node"],
            ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
             "repo.packagist.org", "packagist.org", "api.github.com", "codeload.github.com"),
        ),
    ],
)
def test_network_allowlist_from_ecosystems(ecosystems, expected):
    assert config.network_allowlist(ecosystems) == expected


def test_github_api_hosts_only_for_php():
    for eco in ("python", "node"):
        hosts = config.network_allowlist([eco])
        assert "api.github.com" not in hosts and "codeload.github.com" not in hosts


def test_extra_hosts_appended_and_deduped(home):
    r = load(home, repo(extra_hosts='["mirror.example.com", "pypi.org"]')).repos["ENG"]
    assert r.network_allowlist == ("pypi.org", "files.pythonhosted.org", "mirror.example.com")


def test_github_com_never_in_any_allowlist():
    for n in range(1, len(config.ECOSYSTEMS) + 1):
        assert "github.com" not in config.network_allowlist(config.ECOSYSTEMS[:n])
    with pytest.raises(ValueError, match="never-allowed"):
        config.network_allowlist(["php"], ["github.com"])


@pytest.mark.parametrize("host", ["github.com", "GitHub.com", "github.com."])
def test_extra_hosts_rejects_github_com(home, host):
    msg = issues(home, repo(extra_hosts=f'["{host}"]'))["repos.ENG.extra_hosts"]
    assert "never allowed" in msg and "Fix: remove" in msg


@pytest.mark.parametrize(
    "host",
    ["api.github.com", "codeload.github.com", "*.github.com", "objects.githubusercontent.com", "*.githubusercontent.com"],
)
def test_extra_hosts_rejects_other_github_hosts(home, host):
    msg = issues(home, repo(extra_hosts=f'["{host}"]'))["repos.ENG.extra_hosts"]
    assert "GitHub host" in msg and "php ecosystem" in msg


@pytest.mark.parametrize("host", ["https://pypi.org", "pypi.org:443", "pypi.org/simple", "*", "localhost", "10.0.0.1", ""])
def test_extra_hosts_rejects_non_hostnames(home, host):
    assert "repos.ENG.extra_hosts" in issues(home, repo(extra_hosts=f'["{host}"]'))


def test_extra_hosts_allows_wildcard_subdomain(home):
    assert load(home, repo(extra_hosts='["*.example.com"]')).repos["ENG"].extra_hosts == ("*.example.com",)


# --- deny list (R1, R20, R28, R29) -----------------------------------------


def test_deny_list_is_complete(home):
    cfg = load(home)
    assert cfg.deny_read_paths == (
        home / ".ssh",
        home / ".config" / "gh",
        home / ".aws",
        home / "Library" / "Keychains",
        home / ".claude",
        home / ".codex",
        home / ".gaffer",
        home / ".claude.json",
        home / ".composer",
        home / ".config" / "composer",
        home / "Library" / "Application Support" / "Herd" / "config" / "valet",
        home / "vault",
    )


def test_deny_list_does_not_depend_on_ecosystem(home):
    php = load(home, repo(ecosystems='["php"]', runner='"phpunit"'))
    assert php.deny_read_paths == load(home).deny_read_paths


def test_config_file_is_inside_the_deny_list(home):
    path = config.default_config_path(home)
    assert any(path.is_relative_to(p) for p in config.deny_read_paths(home, home / "vault"))


# --- harness and forbidden paths (R7, design Writer rules) -----------------


@pytest.mark.parametrize(
    "relpath",
    ["conftest.py", "tests/unit/conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini",
     "uv.lock", "requirements-dev.txt", "Makefile", ".github/workflows/ci.yml", ".github/actions/setup/action.yml"],
)
def test_python_harness_defaults(home, relpath):
    assert load(home).repos["ENG"].is_harness_path(relpath)


@pytest.mark.parametrize("relpath", ["package.json", "package-lock.json", "jest.config.ts", "vitest.config.mts", "web/package.json"])
def test_node_harness_defaults(home, relpath):
    r = load(home, repo(ecosystems='["node"]', runner='"jest"')).repos["ENG"]
    assert r.is_harness_path(relpath)
    assert not r.is_harness_path("conftest.py")


@pytest.mark.parametrize("relpath", ["composer.json", "composer.lock", "phpunit.xml", "phpunit.xml.dist"])
def test_php_harness_defaults(home, relpath):
    assert load(home, repo(ecosystems='["php"]', runner='"phpunit"')).repos["ENG"].is_harness_path(relpath)


def test_harness_paths_extend_defaults(home):
    r = load(home, repo(harness_paths='["scripts/**"]')).repos["ENG"]
    assert r.is_harness_path("scripts/test.sh")
    assert r.is_harness_path("conftest.py")
    assert not r.is_harness_path("src/app.py")


@pytest.mark.parametrize("relpath", [".claude/settings.json", "sub/.claude/agents/x.md", ".codex/config.toml", ".mcp.json"])
def test_forbidden_defaults(home, relpath):
    assert load(home).repos["ENG"].is_forbidden_path(relpath)


@pytest.mark.parametrize("relpath", [".github/workflows/ci.yml", ".github/actions/setup/action.yml"])
def test_ci_files_are_harness_not_forbidden(home, relpath):
    """R30: CI edits are allowed with a Tester-Change-Reason trailer and listed in the report."""
    r = load(home).repos["ENG"]
    assert r.is_harness_path(relpath)
    assert not r.is_forbidden_path(relpath)


@pytest.mark.parametrize("job_root", ['"~/.gaffer/jobs"', '"~/.gaffer"', '"~"', '"~/vault/jobs"', '"~/.ssh/x"', '"jobs"'])
def test_job_root_must_be_readable_by_the_sandbox(home, job_root):
    globals_ = f'operator_login = "mfullbrook"\nvault_path = "~/vault"\njob_root = {job_root}\n'
    assert "job_root" in issues(home, globals_=globals_)


def test_job_root_is_outside_every_denied_path(home):
    cfg = load(home)
    assert not any(cfg.job_root.is_relative_to(d) for d in cfg.deny_read_paths)


@pytest.mark.parametrize("key", ["writer_model", "codex_model"])
def test_bad_model_names(home, key):
    globals_ = f'operator_login = "mfullbrook"\nvault_path = "~/vault"\n{key} = "opus --dangerously-skip-permissions"\n'
    assert key in issues(home, globals_=globals_)


def test_forbidden_paths_cannot_be_removed(home):
    r = load(home, repo(forbidden_paths="[]")).repos["ENG"]
    assert r.is_forbidden_path(".mcp.json")
    assert not r.is_forbidden_path("src/app.py")


@pytest.mark.parametrize("key", ["harness_paths", "forbidden_paths"])
@pytest.mark.parametrize("pattern", ["/etc/passwd", "../x", "a/../../b", "~/x"])
def test_path_patterns_must_stay_in_checkout(home, key, pattern):
    assert f"repos.ENG.{key}" in issues(home, repo(**{key: f'["{pattern}"]'}))


# --- validation errors name the field and the fix --------------------------


@pytest.mark.parametrize("key", ["path", "github", "base", "ecosystems", "test_command", "junit_path", "runner", "test_dirs"])
def test_missing_required_repo_field(home, key):
    msg = issues(home, repo(**{key: None}))[f"repos.ENG.{key}"]
    assert "missing." in msg and "Fix: set" in msg


@pytest.mark.parametrize("key", ["operator_login", "vault_path"])
def test_missing_required_global(home, key):
    globals_ = 'operator_login = "mfullbrook"\nvault_path = "~/vault"\n'
    globals_ = "\n".join(line for line in globals_.splitlines() if not line.startswith(key)) + "\n"
    assert "missing." in issues(home, globals_=globals_)[key]


def test_every_issue_is_reported_at_once(home):
    found = issues(home, repo(runner='"mocha"', ecosystems='["ruby"]', test_timeout="0"))
    assert {"repos.ENG.runner", "repos.ENG.ecosystems", "repos.ENG.test_timeout"} <= set(found)


def test_error_message_lists_file_and_problems(home):
    with pytest.raises(ConfigError) as exc:
        load(home, repo(runner='"mocha"'))
    text = str(exc.value)
    assert text.startswith(f"{home / '.gaffer' / 'gaffer.toml'} is invalid (1 problem):")
    assert 'repos.ENG.runner: \'mocha\' is not supported. Fix: set runner to one of "pytest", "jest", "vitest", "phpunit", "generic"' in text


def test_missing_file(home):
    with pytest.raises(ConfigError) as exc:
        config.load_config(home / "nope.toml", home=home)
    assert exc.value.exit_code == 4
    assert "(file): not found. Fix: create" in str(exc.value)


def test_bad_toml(home):
    path = home / ".gaffer" / "gaffer.toml"
    path.write_text("operator_login = \n")
    with pytest.raises(ConfigError) as exc:
        config.load_config(path, home=home)
    assert "(file): not valid TOML" in str(exc.value)


@pytest.mark.parametrize("ecosystems", ['["ruby"]', "[]", '"python"'])
def test_bad_ecosystems(home, ecosystems):
    assert "repos.ENG.ecosystems" in issues(home, repo(ecosystems=ecosystems))


@pytest.mark.parametrize("runner", ["pytest", "jest", "vitest", "phpunit", "generic"])
def test_supported_runners(home, runner):
    eco = config.RUNNERS[runner] or "python"
    assert load(home, repo(runner=f'"{runner}"', ecosystems=f'["{eco}"]')).repos["ENG"].runner == runner


def test_runner_needs_its_ecosystem(home):
    msg = issues(home, repo(runner='"jest"'))["repos.ENG.runner"]
    assert "needs the 'node' ecosystem" in msg and 'add "node" to ecosystems' in msg


def test_generic_runner_with_any_ecosystem(home):
    assert load(home, repo(runner='"generic"', ecosystems='["php"]')).repos["ENG"].runner == "generic"


@pytest.mark.parametrize("key", ["test_timeout", "round_timeout", "check_appear_timeout", "coderabbit_appear_timeout"])
@pytest.mark.parametrize("value", ["0", "-5", "1.5", "true", '"15m"'])
def test_bad_timeouts(home, key, value):
    msg = issues(home, repo(**{key: value}))[f"repos.ENG.{key}"]
    assert "Fix: set" in msg and "seconds" in msg


@pytest.mark.parametrize("dirs", ["[]", '["."]', '["../tests"]', '["/abs/tests"]', '["tests/../.."]', '[""]', '"tests"'])
def test_bad_test_dirs(home, dirs):
    assert "repos.ENG.test_dirs" in issues(home, repo(test_dirs=dirs))


@pytest.mark.parametrize("junit", ['"/tmp/junit.xml"', '"../junit.xml"', '""'])
def test_bad_junit_path(home, junit):
    assert "repos.ENG.junit_path" in issues(home, repo(junit_path=junit))


def test_empty_required_checks_refused_without_allow_no_ci(home):
    msg = issues(home, repo(required_checks="[]"))["repos.ENG.required_checks"]
    assert "allow_no_ci = true" in msg


def test_missing_required_checks_refused_without_allow_no_ci(home):
    assert "repos.ENG.required_checks" in issues(home, repo(required_checks=None))


def test_allow_no_ci_accepts_empty_required_checks(home):
    r = load(home, repo(required_checks="[]", allow_no_ci="true")).repos["ENG"]
    assert r.required_checks == () and r.allow_no_ci is True


def test_allow_no_ci_with_checks_is_contradictory(home):
    assert "repos.ENG.allow_no_ci" in issues(home, repo(allow_no_ci="true"))


def test_skippable_checks_must_be_required(home):
    msg = issues(home, repo(skippable_checks='["lint"]'))["repos.ENG.skippable_checks"]
    assert "'lint' is not in required_checks" in msg


def test_repo_path_must_exist_and_be_a_checkout(home):
    (home / "plain").mkdir()
    assert "does not exist" in issues(home, repo(path='"~/missing"'))["repos.ENG.path"]
    assert "not a git checkout" in issues(home, repo(path='"~/plain"'))["repos.ENG.path"]
    assert "not an absolute path" in issues(home, repo(path='"src/app"'))["repos.ENG.path"]


@pytest.mark.parametrize("github", ['"app"', '"https://github.com/mfullbrook/app"', '"a/b/c"'])
def test_bad_github(home, github):
    assert "repos.ENG.github" in issues(home, repo(github=github))


@pytest.mark.parametrize("base", ['"-main"', '"ma in"', '"a..b"', '"main.lock"', '"feat/"', '"/main"', '""'])
def test_bad_base(home, base):
    assert "repos.ENG.base" in issues(home, repo(base=base))


def test_bad_team_key(home):
    assert "repos.eng" in issues(home, REPO.replace("repos.ENG", "repos.eng"))


def test_no_repos(home):
    assert "repos" in issues(home, "")


def test_unknown_keys_flagged_with_known_keys(home):
    found = issues(home, repo(required_check='["test"]'))
    assert "is not a known setting" in found["repos.ENG.required_check"]
    assert "required_checks" in found["repos.ENG.required_check"]


@pytest.mark.parametrize("key", ["github_token", "api_secret", "password"])
def test_token_keys_point_to_keychain(home, key):
    globals_ = f'operator_login = "mfullbrook"\nvault_path = "~/vault"\n{key} = "x"\n'
    assert "macOS Keychain" in issues(home, globals_=globals_)[key]


@pytest.mark.parametrize("login", ['"@mfullbrook"', '"coderabbitai[bot]"', '"-x"'])
def test_bad_operator_login(home, login):
    globals_ = f'operator_login = {login}\nvault_path = "~/vault"\n'
    assert "operator_login" in issues(home, globals_=globals_)


@pytest.mark.parametrize("name", ["linear", "Linear-MCP", "claude_ai_Linear", "claude.ai Gmail"])
def test_allowed_mcp_never_admits_linear_or_connectors(home, name):
    globals_ = f'operator_login = "mfullbrook"\nvault_path = "~/vault"\nallowed_mcp = ["{name}"]\n'
    assert "allowed_mcp" in issues(home, globals_=globals_)


def test_vault_must_exist(home):
    globals_ = 'operator_login = "mfullbrook"\nvault_path = "~/no-vault"\n'
    assert "does not exist" in issues(home, globals_=globals_)["vault_path"]


def test_config_inside_vault_refused(home):
    path = home / "vault" / "gaffer.toml"
    path.write_text('operator_login = "mfullbrook"\nvault_path = "~/vault"\n' + REPO)
    with pytest.raises(ConfigError) as exc:
        config.load_config(path, home=home)
    assert "(file): gaffer.toml is inside the vault" in str(exc.value)


# --- Keychain --------------------------------------------------------------

TOKEN = "ghp_FAKE0123456789abcdefghijklmnopqrstuv"


class FakeKeychain:
    def __init__(self, items=None):
        self.items = dict(items or {})
        self.calls = []

    def find_generic_password(self, service, account):
        self.calls.append((service, account))
        return self.items.get((service, account))


def test_github_token_from_fake_keychain():
    kc = FakeKeychain({("gaffer", "github-token"): TOKEN})
    secret = config.github_token(kc)
    assert secret.reveal() == TOKEN
    assert kc.calls == [("gaffer", "github-token")]


def test_missing_token_names_the_fix():
    with pytest.raises(KeychainError) as exc:
        config.github_token(FakeKeychain())
    assert exc.value.exit_code == 4
    assert "security add-generic-password -U -s gaffer -a github-token -w" in str(exc.value)


@pytest.mark.parametrize("value", ["", "ghp_abc def", "ghp_abc\nsecond"])
def test_malformed_token_is_not_echoed(value):
    with pytest.raises(KeychainError) as exc:
        config.github_token(FakeKeychain({("gaffer", "github-token"): value}))
    assert "empty or contains whitespace" in str(exc.value)
    if value:
        assert value not in str(exc.value)
        assert "ghp_abc" not in str(exc.value)


def test_secret_never_renders(caplog):
    secret = config.github_token(FakeKeychain({("gaffer", "github-token"): TOKEN}))
    caplog.set_level(logging.DEBUG)
    logging.getLogger("gaffer").debug("token %s %r", secret, secret)
    rendered = [str(secret), repr(secret), f"{secret}", f"{secret!r}", f"{secret:>10}", caplog.text]
    assert not any(TOKEN in r for r in rendered)


def test_secret_cannot_enter_a_child_env():
    secret = Secret(TOKEN)
    # subprocess encodes POSIX env values with os.fsencode, which refuses a Secret.
    with pytest.raises(TypeError):
        os.fsencode(secret)
    with pytest.raises(TypeError):
        import pickle

        pickle.dumps(secret)


def test_lookup_leaves_environment_untouched():
    before = dict(os.environ)
    config.github_token(FakeKeychain({("gaffer", "github-token"): TOKEN}))
    assert dict(os.environ) == before
    assert not any(TOKEN in v for v in os.environ.values())


def test_default_keychain_is_mac_keychain_and_guarded():
    # Without an injected keychain, lookup goes to /usr/bin/security, which conftest blocks.
    with pytest.raises(AssertionError, match="real Keychain"):
        config.github_token()


def fake_security(returncode=0, stdout="", stderr=""):
    calls = []

    def run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return run, calls


def test_mac_keychain_command_and_output():
    run, calls = fake_security(stdout=TOKEN + "\n")
    assert config.MacKeychain(run).find_generic_password("gaffer", "github-token") == TOKEN
    assert calls == [["/usr/bin/security", "find-generic-password", "-s", "gaffer", "-a", "github-token", "-w"]]


def test_mac_keychain_not_found_is_none():
    run, _ = fake_security(returncode=44, stderr="The specified item could not be found in the keychain.")
    assert config.MacKeychain(run).find_generic_password("gaffer", "github-token") is None


def test_mac_keychain_failure_hides_output():
    run, _ = fake_security(returncode=51, stdout=TOKEN, stderr=TOKEN)
    with pytest.raises(KeychainError) as exc:
        config.MacKeychain(run).find_generic_password("gaffer", "github-token")
    assert "exited 51" in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_mac_keychain_timeout_names_the_fix():
    def run(argv):
        raise subprocess.TimeoutExpired(argv, 30)

    with pytest.raises(KeychainError, match="Fix: unlock the login Keychain"):
        config.MacKeychain(run).find_generic_password("gaffer", "github-token")


@pytest.mark.parametrize(
    ("key", "value"),
    [("operator_login", '"mfullbrook\\n"'), ("allowed_skills", '["code-loop\\n"]'), ("writer_model", '"opus\\n"')],
)
def test_trailing_newline_rejected_in_globals(home, key, value):
    globals_ = f'operator_login = "mfullbrook"\nvault_path = "~/vault"\n'
    globals_ = "\n".join(l for l in globals_.splitlines() if not l.startswith(key)) + f"\n{key} = {value}\n"
    assert key in issues(home, globals_=globals_)


@pytest.mark.parametrize(("key", "value"), [("github", '"mfullbrook/app\\n"')])
def test_trailing_newline_rejected_in_repo(home, key, value):
    assert f"repos.ENG.{key}" in issues(home, repo(**{key: value}))


def test_trailing_newline_rejected_in_team_key(home):
    assert any("ENG\n" in f for f in issues(home, '[repos."ENG\\n"]\npath = "~/src/app"\n'))
