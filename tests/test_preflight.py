"""Preflight (R2, R3, R15, R28, R29): positive login checks against gaffer's own config dirs,
refusals, and the evidence recorded per job. Every CLI here is a fake."""

import json

import pytest

from children_fakes import fakes, home, paths, r2_auth_status, write_config  # noqa: F401 (fixtures)
from gaffer import children
from gaffer.children import PreflightError, preflight

CLEAN_ENV = {"HOME": "/Users/mark", "USER": "mark", "LANG": "en_GB.UTF-8", "TERM": "xterm-256color", "TMPDIR": "/tmp"}


def install_clis(fakes, *, auth=None, codex_login="Logged in using ChatGPT\n", gitleaks=True):
    auth = r2_auth_status() if auth is None else auth
    fakes.install("claude", {"rules": [
        {"match": ["--version"], "stdout": "2.1.304 (Claude Code)\n"},
        {"match": ["auth", "status", "--json"], "stdout": auth if isinstance(auth, str) else json.dumps(auth)},
    ]})
    fakes.install("codex", {"rules": [
        {"match": ["--version"], "stdout": "codex-cli 0.160.0\n"},
        {"match": ["login", "status"], "stderr": codex_login},
    ]})
    if gitleaks:
        fakes.install("gitleaks", {"rules": [{"match": ["version"], "stdout": "8.30.0\n"}]})


@pytest.fixture
def cfg_repo(home):
    return write_config(home)


def problems_of(excinfo) -> str:
    return "\n".join(excinfo.value.problems)


def test_passes_on_subscription_logins_and_records_versions(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes)
    report = preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    assert report.claude_version == "2.1.304 (Claude Code)"
    assert report.codex_version == "codex-cli 0.160.0"
    assert report.gitleaks_version == "8.30.0"
    assert report.codex_login == "Logged in using ChatGPT"
    assert report.claude_auth["authMethod"] == "claude.ai"
    assert report.claude_auth["apiProvider"] == "firstParty"
    assert report.srt_version is None  # no install_command


def test_status_output_is_redacted(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes, auth={**r2_auth_status(), "email": "mark@example.com", "orgId": "org-123", "orgName": "Mark's"})
    report = preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    text = json.dumps(report.claude_auth)
    assert "mark@example.com" not in text and "org-123" not in text and "Mark's" not in text


def test_probes_run_in_gaffer_config_dirs_with_a_stripped_env(cfg_repo, paths, fakes, monkeypatch):
    cfg, repo = cfg_repo
    install_clis(fakes)
    monkeypatch.setenv("GH_TOKEN", "ghp_not_for_children")
    monkeypatch.setenv("LINEAR_API_KEY", "lin_not_for_children")
    preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    for name in ("claude", "codex", "gitleaks"):
        for call in fakes.calls(name):
            assert set(call["env"]) <= children.ALLOWED_ENV_KEYS | {"__CF_USER_TEXT_ENCODING"}
            assert call["env"]["CLAUDE_CONFIG_DIR"] == str(paths.home / ".gaffer" / "claude")
            assert call["env"]["CODEX_HOME"] == str(paths.home / ".gaffer" / "codex")
            assert "GH_TOKEN" not in call["env"] and "LINEAR_API_KEY" not in call["env"]
            assert "--bare" not in call["argv"]
            assert call["stdin_devnull"]
    assert ["auth", "status", "--json"] in [c["argv"] for c in fakes.calls("claude")]
    assert ["login", "status"] in [c["argv"] for c in fakes.calls("codex")]


@pytest.mark.parametrize("change, needle", [
    ({"loggedIn": False}, "loggedIn"),
    ({"authMethod": "console"}, "authMethod"),
    ({"authMethod": "none"}, "authMethod"),
    ({"apiProvider": "bedrock"}, "apiProvider"),
    ({"apiProvider": "vertex"}, "apiProvider"),
    ({"configDirectory": "~/.claude"}, "configDirectory"),
])
def test_refuses_non_subscription_claude_login(cfg_repo, paths, fakes, change, needle):
    cfg, repo = cfg_repo
    install_clis(fakes, auth={**r2_auth_status(), **change})
    with pytest.raises(PreflightError) as e:
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    assert needle in problems_of(e)
    assert e.value.exit_code == 4


def test_refuses_when_claude_auth_field_is_missing(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    auth = r2_auth_status()
    del auth["apiProvider"]  # a CLI update that renames a field fails closed (R2)
    install_clis(fakes, auth=auth)
    with pytest.raises(PreflightError, match="apiProvider"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


def test_refuses_unparseable_auth_status(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes, auth="Not logged in\n")
    with pytest.raises(PreflightError, match="no JSON object"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


@pytest.mark.parametrize("login", ["Logged in using an API key - sk-proj-***\n", "Not logged in\n", ""])
def test_refuses_non_chatgpt_codex_login(cfg_repo, paths, fakes, login):
    cfg, repo = cfg_repo
    install_clis(fakes, codex_login=login)
    with pytest.raises(PreflightError, match="codex login status"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


@pytest.mark.parametrize("key, value", [
    ("CLAUDE_CODE_SIMPLE", "1"),
    ("CLAUDE_CODE_SIMPLE", ""),
    ("ANTHROPIC_API_KEY", "sk-ant-x"),
    ("ANTHROPIC_AUTH_TOKEN", "x"),
    ("ANTHROPIC_BASE_URL", "https://proxy.example"),
    ("OPENAI_API_KEY", "sk-x"),
    ("OPENAI_BASE_URL", "https://proxy.example"),
    ("CLAUDE_CODE_USE_BEDROCK", "1"),
    ("CLAUDE_CODE_USE_VERTEX", "1"),
    ("CODEX_API_KEY", "x"),
])
def test_refuses_billing_switches_in_env(cfg_repo, paths, fakes, key, value):
    cfg, repo = cfg_repo
    install_clis(fakes)
    with pytest.raises(PreflightError) as e:
        preflight(cfg, repo, paths=paths, environ={**CLEAN_ENV, key: value})
    assert key in problems_of(e)


def test_refuses_without_gitleaks(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes, gitleaks=False)
    with pytest.raises(PreflightError, match="gitleaks not found.*brew install gitleaks"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


def test_refuses_missing_config_dirs(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes)
    (paths.home / ".gaffer" / "codex").rmdir()
    with pytest.raises(PreflightError, match="CODEX_HOME dir"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


@pytest.mark.parametrize("settings, needle", [
    ({"env": {"ANTHROPIC_BASE_URL": "https://proxy"}}, "env block"),
    ({"apiKeyHelper": "/usr/local/bin/key"}, "apiKeyHelper"),
    ({"awsCredentialExport": "/bin/creds"}, "awsCredentialExport"),
    ({"forceLoginMethod": "console"}, "Console"),
])
def test_refuses_managed_claude_settings(cfg_repo, paths, fakes, settings, needle):
    cfg, repo = cfg_repo
    install_clis(fakes)
    paths.claude_managed_dir.mkdir(parents=True)
    (paths.claude_managed_dir / "managed-settings.json").write_text(json.dumps(settings))
    with pytest.raises(PreflightError, match=needle):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


def test_refuses_managed_settings_dropin_and_unreadable_files(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes)
    d = paths.claude_managed_dir / "managed-settings.d"
    d.mkdir(parents=True)
    (d / "10-corp.json").write_text(json.dumps({"apiKeyHelper": "x"}))
    (paths.claude_managed_dir / "managed-settings.json").write_text("{not json")
    with pytest.raises(PreflightError) as e:
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    text = problems_of(e)
    assert "10-corp.json sets apiKeyHelper" in text
    assert "could not be read" in text


def test_allows_harmless_managed_settings(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes)
    paths.claude_managed_dir.mkdir(parents=True)
    (paths.claude_managed_dir / "managed-settings.json").write_text(json.dumps({"permissions": {"deny": ["WebFetch"]}}))
    preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


def test_refuses_gaffer_user_settings_with_env(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes)
    (paths.claude_config_dir / "settings.json").write_text(json.dumps({"env": {"FOO": "1"}}))
    with pytest.raises(PreflightError, match="settings.json sets an env block"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


@pytest.mark.parametrize("toml, needle", [
    ('model_provider = "azure"\n', "model_provider"),
    ('[model_providers.proxy]\nbase_url = "https://proxy"\n', "model_providers"),
    ('[profiles.x]\nmodel_provider = "oss"\n', "profiles.x.model_provider"),
    ('openai_base_url = "https://proxy"\n', "openai_base_url"),
    ('preferred_auth_method = "apikey"\n', "preferred_auth_method"),
    ('[mcp_servers.linear]\ncommand = "npx"\n', "mcp_servers"),
    ('[projects."/Users/mark/src/app"]\ntrust_level = "trusted"\n', "trusts a project"),
    ("not = [valid", "could not be read"),
])
def test_refuses_codex_config(cfg_repo, paths, fakes, toml, needle):
    cfg, repo = cfg_repo
    install_clis(fakes)
    (paths.codex_home / "config.toml").write_text(toml)
    with pytest.raises(PreflightError, match=needle):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


def test_refuses_managed_codex_config(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes)
    paths.codex_managed_dir.mkdir(parents=True)
    (paths.codex_managed_dir / "managed_config.toml").write_text('model_provider = "azure"\n')
    with pytest.raises(PreflightError, match="managed_config.toml sets model_provider"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


def test_allows_plain_codex_config(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes)
    (paths.codex_home / "config.toml").write_text('model = "gpt-6"\n[features]\nweb_search = false\n')
    preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


def test_collects_every_problem_at_once(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes, auth={**r2_auth_status(), "authMethod": "console"}, codex_login="Not logged in", gitleaks=False)
    with pytest.raises(PreflightError) as e:
        preflight(cfg, repo, paths=paths, environ={**CLEAN_ENV, "CLAUDE_CODE_SIMPLE": "1"})
    text = problems_of(e)
    for needle in ("CLAUDE_CODE_SIMPLE", "authMethod", "codex login status", "gitleaks"):
        assert needle in text
    assert str(e.value).startswith("preflight refused (4 problems)")


# --- install_command prerequisites (R28, R29) ---


def install_srt(paths, fakes, version="0.0.79"):
    fakes.install(paths.srt)
    paths.srt_package_json.parent.mkdir(parents=True, exist_ok=True)
    paths.srt_package_json.write_text(json.dumps({"name": "@anthropic-ai/sandbox-runtime", "version": version}))


def test_install_command_needs_pinned_srt_and_node(home, paths, fakes):
    cfg, repo = write_config(home, install_command="uv sync")
    install_clis(fakes)
    with pytest.raises(PreflightError) as e:
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    assert "sandbox-runtime not found" in problems_of(e)
    install_srt(paths, fakes, version="0.0.81")
    with pytest.raises(PreflightError, match=r"'0.0.81', pinned 0.0.79"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    install_srt(paths, fakes)
    with pytest.raises(PreflightError, match="node not found"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    fakes.install("node")
    assert preflight(cfg, repo, paths=paths, environ=CLEAN_ENV).srt_version == "0.0.79"


def test_php_repo_needs_herd(home, paths, fakes):
    cfg, repo = write_config(home, ecosystems=("php",), runner="phpunit")
    install_clis(fakes)
    with pytest.raises(PreflightError, match="Herd"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    for tool in ("php", "composer"):
        fakes.install(paths.herd_bin / tool)
    preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)


# --- MCP definitions (R3, R30) ---


def test_allowed_mcp_needs_a_definition(home, paths, fakes):
    cfg, repo = write_config(home, allowed_mcp=["context7"])
    install_clis(fakes)
    with pytest.raises(PreflightError, match="mcp.json does not exist"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    paths.mcp_config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    with pytest.raises(PreflightError, match=r"no definition for allowed_mcp \['context7'\]"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    paths.mcp_config.write_text(json.dumps({"mcpServers": {"context7": {"type": "http", "url": "https://mcp.context7.com/mcp"}}}))
    assert preflight(cfg, repo, paths=paths, environ=CLEAN_ENV).mcp_servers == ("context7",)


# --- Plugin block list (R31) ---


def test_refuses_a_malformed_plugin_block_list(cfg_repo, paths, fakes):
    cfg, repo = cfg_repo
    install_clis(fakes)
    paths.blocked_plugins.write_text('{"plugin_keys": ["ok@synced"]}')
    preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
    paths.blocked_plugins.write_text("{")
    with pytest.raises(PreflightError, match="blocked-plugins.json must be JSON"):
        preflight(cfg, repo, paths=paths, environ=CLEAN_ENV)
