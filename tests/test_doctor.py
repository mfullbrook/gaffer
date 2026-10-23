"""`gaffer doctor` (R18 decision A, R20, R28, R29, R31): every probe's pass and fail path, the
version-change trigger in `gaffer run`'s preflight, the exit-4 refusal naming the probe, redacted
storage, and crashes counted as failures. Every CLI here is a fake (tests/doctor_fakes.py)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from children_fakes import fakes, home, paths, write_config  # noqa: F401 (fixtures)
from doctor_fakes import DoctorRig, by_name, failed
from gaffer import children as ch
from gaffer import doctor
from gaffer import runner as rn
from gaffer import store as st
from runner_fakes import World

DENY_MSG = "<tool_use_error>File is in a directory that is denied by your permission settings.</tool_use_error>"


@pytest.fixture
def rig(home, paths, fakes) -> DoctorRig:
    return DoctorRig(home, fakes.bindir, paths)


# --- the whole suite ------------------------------------------------------------------------


def test_every_probe_passes_against_working_controls(rig):
    report = rig.run()
    assert failed(report) == [], doctor.format_report(report)
    assert [p.name for p in report.probes] == list(doctor.PROBES)
    assert report.passed
    assert report.claude_version == "2.1.308 (Claude Code)" and report.codex_version == "codex-cli 0.160.0"
    assert report.srt_version == ch.SRT_VERSION == report.srt_installed
    # One Claude child and one codex exec child: the probes that cost quota run once each.
    assert rig.model_calls() == {"claude": 1, "codex": 1}


def test_claude_child_uses_the_writer_flag_set_with_haiku(rig):
    rig.run()
    call = next(c for c in rig.calls("claude") if "-p" in c["argv"])
    argv = call["argv"]
    assert "--bare" not in argv
    assert argv[argv.index("--model") + 1] == doctor.DOCTOR_CLAUDE_MODEL
    assert argv[argv.index("--tools") + 1] == ch.WRITER_TOOLS
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in argv and argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert call["env"]["CLAUDE_CONFIG_DIR"] == str(rig.paths.claude_config_dir)
    assert call["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert not any(k.startswith("ANTHROPIC_") for k in call["env"])
    assert Path(argv[argv.index("--settings") + 1]).is_relative_to(rig.home / ".gaffer")  # children can't read it


def test_codex_child_uses_the_tester_flag_set(rig):
    rig.run()
    argv = next(c for c in rig.calls("codex") if c["argv"][:1] == ["exec"])["argv"]
    assert 'web_search="disabled"' in argv and "project_doc_max_bytes=0" in argv
    assert 'default_permissions="gaffer_ro"' in argv
    assert f'model_reasoning_effort="{doctor.DOCTOR_CODEX_EFFORT}"' in argv
    for feature in ch.CODEX_DISABLED_FEATURES:
        assert feature in argv
    sandbox = next(c for c in rig.calls("codex") if c["argv"][:1] == ["sandbox"])["argv"]
    assert "-P" in sandbox and sandbox[sandbox.index("-P") + 1] == ch.CODEX_TEST_PROFILE


def test_probe_dirs_are_disposable_and_outside_any_real_repo(rig, home):
    rig.run()
    call = next(c for c in rig.calls("claude") if "-p" in c["argv"])
    cwd = Path(call["cwd"])
    assert cwd.name == "repo" and cwd.parent.name == "job" and cwd.parent.parent.name.startswith("doctor-")
    assert not cwd.exists()  # removed afterwards
    assert not (home / doctor.DOCTOR_DIR).exists() or not any((home / doctor.DOCTOR_DIR).iterdir())


def test_with_a_config_the_probes_run_under_job_root_and_probe_the_vault(rig, home):
    cfg, _ = write_config(home)
    report = rig.run(cfg)
    assert report.passed, doctor.format_report(report)
    call = next(c for c in rig.calls("claude") if "-p" in c["argv"])
    assert Path(call["cwd"]).is_relative_to(cfg.job_root)
    prompt = call["argv"][call["argv"].index("-p") + 1]
    assert f"- {cfg.vault_path}" in prompt  # file-tool Read of the real vault entry (no file written into it)
    assert list(cfg.vault_path.iterdir()) == []
    assert "list ~/vault" in by_name(report)["claude_read"].evidence


# --- env, versions, logins, config dirs --------------------------------------------------------


def test_api_key_in_env_fails_and_no_model_child_starts(rig):
    report = rig.run(environ={"HOME": str(rig.home), "ANTHROPIC_API_KEY": "sk-ant-not-real", "TMPDIR": "/tmp"})
    res = by_name(report)
    assert not res["env"].passed and "ANTHROPIC_API_KEY" in res["env"].evidence
    assert "sk-ant" not in json.dumps(report.to_dict())
    for n in doctor.CLAUDE_PROBES + doctor.CODEX_PROBES:
        assert not res[n].passed and "env failed" in res[n].evidence
    assert rig.model_calls() == {"claude": 0, "codex": 0}


def test_missing_claude_cli_is_a_failure_with_a_fix(rig):
    (rig.bindir / "claude").unlink()
    report = rig.run()
    res = by_name(report)
    assert not res["versions"].passed and "claude not found" in res["versions"].evidence and res["versions"].fix
    assert not res["claude_auth"].passed and "claude auth login" in res["claude_auth"].fix
    assert all(not res[n].passed for n in doctor.CLAUDE_PROBES)
    assert res["codex_isolation"].passed  # Codex probes don't depend on claude
    assert report.claude_version == "unknown"


@pytest.mark.parametrize("change, needle", [
    ({"authMethod": "console"}, "authMethod"),
    ({"apiProvider": "bedrock"}, "apiProvider"),
    ({"loggedIn": False}, "loggedIn"),
    ({"configDirectory": "/Users/mark/.claude"}, "configDirectory"),
])
def test_claude_not_on_the_subscription_fails_and_no_claude_child_starts(rig, change, needle):
    auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
            "configDirectory": str(rig.paths.claude_config_dir), **change}
    rig.behave("claude", {"auth": auth})
    res = by_name(rig.run())
    assert not res["claude_auth"].passed and needle in res["claude_auth"].evidence
    assert "claude auth login --claudeai" in res["claude_auth"].fix
    assert all(not res[n].passed and "claude_auth failed" in res[n].evidence for n in doctor.CLAUDE_PROBES)
    assert rig.model_calls()["claude"] == 0
    assert res["codex_isolation"].passed


def test_codex_api_key_login_fails_and_no_codex_child_starts(rig):
    rig.behave("codex", {"login": "Logged in using an API key - sk-proj-abcdefghijklmnopqrstuv"})
    report = rig.run()
    res = by_name(report)
    assert not res["codex_auth"].passed and "codex login --device-auth" in res["codex_auth"].fix
    assert "sk-proj-abcdefghijklmnop" not in json.dumps(report.to_dict())
    assert all(not res[n].passed for n in doctor.CODEX_PROBES)
    assert rig.model_calls()["codex"] == 0
    assert res["test_read"].passed  # codex sandbox runs no model and needs no login


def test_refused_settings_in_the_gaffer_config_dir_fail_config_dirs(rig):
    (rig.paths.claude_config_dir / "settings.json").write_text(json.dumps({"apiKeyHelper": "/bin/echo key"}))
    res = by_name(rig.run())
    assert not res["config_dirs"].passed and "apiKeyHelper" in res["config_dirs"].evidence
    assert rig.model_calls() == {"claude": 0, "codex": 0}


def test_trusted_project_in_codex_config_fails_config_dirs(rig):
    (rig.paths.codex_home / "config.toml").write_text('[projects."/tmp/x"]\ntrust_level = "trusted"\n')
    assert not by_name(rig.run())["config_dirs"].passed


# --- the Claude child ---------------------------------------------------------------------------


def test_plugin_in_the_init_event_is_recorded_and_relaunched_once(rig):
    """R31: a newly synced plugin is recorded in the block list and the child relaunched once."""
    rig.behave("claude", {"runs": [{"init": {"plugins": [{"name": "evil", "source": "evil@synced"}]}}, {}]})
    report = rig.run()
    assert report.passed, doctor.format_report(report)
    assert rig.model_calls()["claude"] == 2
    assert "evil@synced" in ch.load_blocked_plugins(rig.paths.blocked_plugins)
    assert "relaunched once" in by_name(report)["claude_isolation"].evidence
    first, second = (c["argv"] for c in rig.calls("claude") if "-p" in c["argv"])
    assert first[first.index("--session-id") + 1] != second[second.index("--session-id") + 1]  # a fresh session ID


def test_plugin_still_loaded_after_the_relaunch_fails_isolation(rig):
    plugin = {"init": {"plugins": [{"name": "evil", "source": "evil@synced"}]}}
    rig.behave("claude", {"runs": [plugin, plugin]})
    res = by_name(rig.run())
    assert not res["claude_isolation"].passed and "evil@synced" in res["claude_isolation"].evidence
    assert all(not res[n].passed for n in doctor.CLAUDE_PROBES)
    assert rig.model_calls()["claude"] == 2


@pytest.mark.parametrize("init, needle", [
    ({"mcp_servers": [{"name": "claude.ai Linear", "status": "failed"}]}, "MCP servers not in allowed_mcp"),
    ({"apiKeySource": "ANTHROPIC_API_KEY"}, "apiKeySource"),
])
def test_connectors_or_an_api_key_in_the_init_event_fail_isolation(rig, init, needle):
    rig.behave("claude", {"init": init})
    res = by_name(rig.run())
    assert not res["claude_isolation"].passed and needle in res["claude_isolation"].evidence
    assert rig.model_calls()["claude"] == 1  # no relaunch: only a plugin-only failure relaunches (R31)


@pytest.mark.parametrize("init, needle", [
    ({"tools": ["Bash", "Read", "mcp__claude_ai_Linear__list_issues"]}, "connector tools"),
    ({"tools": ["Bash", "Read", "WebFetch"]}, "tools outside the writer set"),
    ({"permissionMode": "bypassPermissions"}, "permissionMode"),
])
def test_init_tools_and_mode_are_checked(rig, init, needle):
    rig.behave("claude", {"init": init})
    res = by_name(rig.run())
    assert not res["claude_isolation"].passed and needle in res["claude_isolation"].evidence


def test_session_transcript_must_land_in_the_gaffer_config_dir(rig):
    rig.behave("claude", {"no_transcript": True})
    res = by_name(rig.run())
    assert not res["claude_isolation"].passed and "no session transcript" in res["claude_isolation"].evidence


def test_session_transcript_in_dot_claude_fails(rig):
    rig.behave("claude", {"transcript_in_dot_claude": True})
    res = by_name(rig.run())
    assert not res["claude_isolation"].passed and "~/.claude/projects" in res["claude_isolation"].evidence


def test_synced_skills_are_accepted_and_listed(rig):
    rig.behave("claude", {"init": {"skills": ["code-loop", "anthropic-skills:docs"]}})
    res = by_name(rig.run())
    assert res["claude_isolation"].passed and "anthropic-skills:docs" in res["claude_isolation"].evidence


@pytest.mark.parametrize("needle, probe", [
    ("/.ssh/config", "claude_read"),
    (".claude.json", "claude_read"),
    ("canary/token", "claude_read"),
    ("/vault", "claude_read"),
])
def test_a_readable_credential_under_the_bash_sandbox_fails_claude_read(rig, needle, probe):
    rig.behave("claude", {"outcomes": {needle: "ALLOWED"}})
    report = rig.run()
    assert failed(report) == [probe] and "want DENIED" in by_name(report)[probe].evidence
    assert rig.model_calls()["claude"] == 1  # a real control failure is never retried


def test_an_unreadable_job_file_fails_claude_read(rig):
    """The positive control: a sandbox that denies everything would make every DENIED vacuous."""
    rig.behave("claude", {"outcomes": {"README.md": "DENIED"}})
    assert failed(rig.run()) == ["claude_read"]


@pytest.mark.parametrize("needle, outcome", [("pypi.org", "000"), ("example.com", "200"), ("api.github.com", "200"),
                                             ("//github.com", "301")])
def test_network_outside_the_allowlist_or_a_dead_allowlist_fails_claude_network(rig, needle, outcome):
    rig.behave("claude", {"outcomes": {needle: outcome}})
    assert failed(rig.run()) == ["claude_network"]


@pytest.mark.parametrize("needle", ["/outside/", "/.gaffer-doctor-", "-012345/doctor-claude_write"])  # last: in ~/.gaffer
def test_a_write_outside_the_job_fails_claude_write_and_is_cleaned_up(rig, home, needle):
    rig.behave("claude", {"outcomes": {needle: "ALLOWED"}})
    report = rig.run()
    assert failed(report) == ["claude_write"]
    assert "files created outside the job" in by_name(report)["claude_write"].evidence
    assert not list(home.glob(".gaffer-doctor-*"))


def test_a_job_dir_the_writer_cannot_write_fails_claude_write(rig):
    rig.behave("claude", {"outcomes": {"/job/.doctor-": "DENIED"}})
    assert failed(rig.run()) == ["claude_write"]


def test_the_write_tool_succeeding_outside_the_job_fails_claude_write(rig):
    rig.behave("claude", {"write_result": [False, "File created successfully"]})
    res = by_name(rig.run())
    assert not res["claude_write"].passed and "not refused" in res["claude_write"].evidence


@pytest.mark.parametrize("needle", [".claude.json", "/.ssh", "canary/token", "canary-note.md"])
def test_a_file_tool_read_that_succeeds_fails_claude_file_read(rig, needle):
    rig.behave("claude", {"read_results": {needle: [False, "1\tcontents"]}})
    report = rig.run()
    assert failed(report) == ["claude_file_read"]
    assert "READ" in by_name(report)["claude_file_read"].evidence
    assert "contents" not in json.dumps(report.to_dict())


def test_a_file_tool_read_refused_only_for_lack_of_a_grant_fails_claude_file_read(rig):
    """R29: without an exact-path rule ~/.claude.json is refused only because acceptEdits has no grant.
    That isn't the deny rule, so the probe fails."""
    rig.behave("claude", {"read_results": {".claude.json": [True, "Claude requested permissions to read from "
                                                                   "/x/.claude.json, but you haven't granted it yet."]}})
    report = rig.run()
    assert failed(report) == ["claude_file_read"]
    assert "refused without a deny rule" in by_name(report)["claude_file_read"].evidence


def test_every_deny_entry_is_read_with_the_file_tool(rig, home):
    rig.run()
    prompt = next(c for c in rig.calls("claude") if "-p" in c["argv"])["argv"]
    prompt = prompt[prompt.index("-p") + 1]
    for entry in (".ssh", ".claude.json", ".claude", ".gaffer"):
        assert f"- {home / entry}\n" in prompt
    assert f"- {home / '.aws'}\n" not in prompt  # absent entries aren't probed


def test_a_skipped_step_gets_one_fresh_attempt(rig):
    rig.behave("claude", {"runs": [{"skip_reads": True}, {}]})
    report = rig.run()
    assert report.passed, doctor.format_report(report)
    assert rig.model_calls()["claude"] == 2
    assert "second attempt" in by_name(report)["claude_file_read"].evidence


def test_steps_skipped_twice_fail_as_not_attempted(rig):
    rig.behave("claude", {"skip_reads": True, "skip_write": True})
    res = by_name(rig.run())
    assert rig.model_calls()["claude"] == 2
    assert not res["claude_file_read"].passed and "not attempted" in res["claude_file_read"].evidence
    assert not res["claude_write"].passed and "not attempted" in res["claude_write"].evidence


def test_a_real_failure_is_never_retried_even_when_a_step_was_skipped(rig):
    rig.behave("claude", {"skip_reads": True, "outcomes": {"/.ssh/config": "ALLOWED"}})
    rig.behave("codex", {"result": None, "exec_outcomes": {"/.ssh/config": "ALLOWED"}})
    report = rig.run()
    assert rig.model_calls() == {"claude": 1, "codex": 1}
    assert {"claude_read", "claude_file_read", "codex_read", "codex_web_search"} <= set(failed(report))


def test_a_skipped_bash_step_fails_every_bash_probe(rig):
    rig.behave("claude", {"skip_bash": True})
    report = rig.run()
    assert set(failed(report)) == {"claude_read", "claude_write", "claude_network"}
    assert "no result" in by_name(report)["claude_read"].evidence


# --- the Codex exec child -----------------------------------------------------------------------


@pytest.mark.parametrize("needle, probe", [
    ("/.ssh/config", "codex_read"),
    (".claude.json", "codex_read"),
    ("canary/token", "codex_read"),
    ("/job/.doctor-codex_write", "codex_write"),   # gaffer_ro: even the job dir is read-only
    ("/outside/", "codex_write"),
    ("pypi.org", "codex_network"),                 # gaffer_ro: no network at all
    ("example.com", "codex_network"),
])
def test_codex_child_sandbox_failures(rig, needle, probe):
    value = "200" if probe == "codex_network" else "ALLOWED"
    rig.behave("codex", {"exec_outcomes": {needle: value}})
    report = rig.run()
    assert failed(report) == [probe], doctor.format_report(report)
    assert rig.model_calls()["codex"] == 1


def test_a_web_search_event_fails_codex_web_search(rig):
    rig.behave("codex", {"extra_items": [{"id": "ws", "type": "web_search", "query": "sandbox-runtime npm"}],
                         "result": {"ran_script": True, "web_search_tool_available": True, "web_search_used": True}})
    report = rig.run()
    assert failed(report) == ["codex_web_search"]
    assert "web_search events" in by_name(report)["codex_web_search"].evidence


def test_a_child_reporting_a_web_search_tool_fails_codex_web_search(rig):
    rig.behave("codex", {"result": {"ran_script": True, "web_search_tool_available": True, "web_search_used": False}})
    assert failed(rig.run()) == ["codex_web_search"]


def test_no_structured_codex_result_is_retried_once_then_fails(rig):
    rig.behave("codex", {"result": None})
    res = by_name(rig.run())
    assert rig.model_calls()["codex"] == 2
    assert not res["codex_web_search"].passed and "could not be probed" in res["codex_web_search"].evidence


def test_a_skipped_codex_script_gets_one_fresh_attempt(rig):
    rig.behave("codex", {"runs": [{"skip_script": True}, {}]})
    report = rig.run()
    assert report.passed, doctor.format_report(report)
    assert rig.model_calls()["codex"] == 2


@pytest.mark.parametrize("behaviour, needle", [
    ({"extra_items": [{"id": "m", "type": "mcp_tool_call", "server": "linear"}]}, "MCP tool call"),
    ({"no_rollout": True}, "no rollout"),
    ({"rollout_in_dot_codex": True}, "~/.codex/sessions"),
    ({"world_state": {"agents_md": {"text": "KIWI"}}}, "agents_md"),
    ({"world_state": {"skills": {"includeInstructions": True}}}, "skills"),
    ({"world_state": {"plugins_instructions": True}}, "plugins_instructions"),
    ({"exec_exit": 1}, "exited 1"),
])
def test_codex_isolation_failures(rig, behaviour, needle):
    rig.behave("codex", behaviour)
    res = by_name(rig.run())
    assert not res["codex_isolation"].passed and needle in res["codex_isolation"].evidence


# --- gaffer's own runs: test_command (codex sandbox) and install_command (srt) -------------------


@pytest.mark.parametrize("needle, value, probe", [
    ("/.ssh/config", "ALLOWED", "test_read"),
    ("canary/token", "ALLOWED", "test_read"),
    ("/job/.doctor-test_write", "DENIED", "test_write"),   # test runs must write their JUnit file
    ("/.gaffer-doctor-", "ALLOWED", "test_write"),
    ("pypi.org", "200", "test_network"),                   # test runs stay network-off
])
def test_test_sandbox_failures(rig, needle, value, probe):
    rig.behave("codex", {"sandbox_outcomes": {needle: value}})
    assert failed(rig.run()) == [probe]


@pytest.mark.parametrize("needle, value, probe", [
    ("/.ssh/config", "ALLOWED", "srt_read"),
    ("/vault", "ALLOWED", "srt_read"),
    ("/outside/", "ALLOWED", "srt_write"),
    ("pypi.org", "000", "srt_network"),
    ("github.com", "200", "srt_network"),
])
def test_srt_sandbox_failures(rig, needle, value, probe):
    rig.behave(rig.paths.srt, {"outcomes": {needle: value}})
    assert failed(rig.run()) == [probe]


def test_srt_missing_fails_with_the_install_line(rig):
    rig.paths.srt.unlink()
    report = rig.run()
    res = by_name(report)
    assert not res["srt_install"].passed and "npm install" in res["srt_install"].fix
    assert ch.SRT_VERSION in res["srt_install"].fix
    assert all(not res[n].passed and "srt_install failed" in res[n].evidence for n in doctor.SRT_PROBES)


def test_srt_off_the_pin_fails(rig):
    rig.paths.srt_package_json.write_text(json.dumps({"version": "0.0.81"}))
    report = rig.run()
    assert set(failed(report)) == {"srt_install", *doctor.SRT_PROBES}
    assert "0.0.81" in by_name(report)["srt_install"].evidence and report.srt_installed == "0.0.81"


# --- crashes, storage and redaction ----------------------------------------------------------------


def test_a_probe_that_crashes_is_a_failure_not_a_skip(rig):
    def boom(ctx):
        raise RuntimeError("socket exploded for mark@example.com")

    groups = [(boom if fn is doctor.probe_codex else fn, names) for fn, names in doctor.GROUPS]
    report = rig.run(groups=groups)
    res = by_name(report)
    assert set(failed(report)) == set(doctor.CODEX_PROBES)
    assert all("probe crashed: RuntimeError" in res[n].evidence for n in doctor.CODEX_PROBES)
    assert "mark@example.com" not in json.dumps(report.to_dict())
    assert not report.passed


def test_a_crash_after_partial_results_keeps_them_and_fails_the_rest(rig):
    def half(ctx):
        ctx.put(doctor.ProbeResult("srt_read", True, "ok"))
        raise OSError("disk full")

    groups = [(half if fn is doctor.probe_srt else fn, names) for fn, names in doctor.GROUPS]
    res = by_name(rig.run(groups=groups))
    assert res["srt_read"].passed and not res["srt_write"].passed and not res["srt_network"].passed


def test_results_are_stored_redacted_with_all_three_versions(rig, tmp_path):
    rig.behave("codex", {"login": "Logged in using an API key for mark@example.com sk-proj-abcdefghijklmnopqrstuvwx"})
    report = rig.run()
    with st.Store(tmp_path / "state.db") as s:
        row = doctor.record(s, report)
        assert (row.claude_version, row.codex_version, row.srt_version) == ("2.1.308 (Claude Code)", "codex-cli 0.160.0",
                                                                            ch.SRT_VERSION)
        assert row.passed is False or row.passed == 0
        text = json.dumps(row.results)
        assert "mark@example.com" not in text and "sk-proj-abcdefghijklmnop" not in text and "org-1234" not in text
        names = {p["name"]: p for p in row.results["probes"]}
        assert set(names) == set(doctor.PROBES) and names["codex_auth"]["passed"] is False
        assert s.doctor_needed("2.1.308 (Claude Code)", "codex-cli 0.160.0", srt_version=ch.SRT_VERSION)


def test_a_pass_is_stored_and_satisfies_doctor_needed(rig, tmp_path):
    report = rig.run()
    with st.Store(tmp_path / "state.db") as s:
        doctor.record(s, report)
        assert not doctor.needed(s, "2.1.308 (Claude Code)", "codex-cli 0.160.0")
        assert doctor.needed(s, "2.1.309 (Claude Code)", "codex-cli 0.160.0")


def test_scrub_redacts_emails_and_tokens():
    out = doctor.scrub({"a": "mail mark@example.com", "b": ["ghp_abcdefghijklmnopqrstuvwxyz0123"],
                        "email": "x", "c": "sk-ant-oat01-abcdefghijklmnopqrstu", "d": "~/.gaffer/doctor-claude_write-0123456789abcdef"})
    text = json.dumps(out)
    assert "mark@example.com" not in text and "ghp_" not in text and "sk-ant" not in text
    assert out["email"] == "<redacted>"
    assert out["d"] == "~/.gaffer/doctor-claude_write-0123456789abcdef"  # paths survive


# --- gaffer run's preflight (R18 decision A, R31) -----------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_first_run_runs_doctor_and_records_the_pass(world):
    code, out = world.run()
    assert code == rn.EXIT_READY, out
    assert world.children.doctor_calls == 1
    assert "running doctor (R18): no passing doctor run is recorded" in out
    with world.store() as s:
        last = s.last_doctor_pass()
        assert (last.claude_version, last.codex_version, last.srt_version) == (*world.children.versions, ch.SRT_VERSION)


def test_no_rerun_when_versions_match_the_last_pass(world):
    world.run()
    world.run("ENG-2")
    assert world.children.doctor_calls == 1


@pytest.mark.parametrize("which", ["claude", "codex"])
def test_a_cli_version_change_reruns_doctor(world, which):
    world.run()
    c, x = world.children.versions
    world.children.versions = ("2.1.309 (Claude Code)", x) if which == "claude" else (c, "codex-cli 0.161.0")
    code, out = world.run("ENG-2")
    assert world.children.doctor_calls == 2, out
    assert ("Claude Code 2.1.304 (Claude Code) -> 2.1.309" if which == "claude" else "Codex codex-cli 0.160.0 -> codex-cli 0.161.0") in out


def test_an_srt_pin_change_reruns_doctor(world, monkeypatch):
    world.run()
    monkeypatch.setattr(ch, "SRT_VERSION", "0.0.80")
    code, out = world.run("ENG-2")
    assert world.children.doctor_calls == 2
    assert f"sandbox-runtime pin 0.0.79 -> 0.0.80" in out
    with world.store() as s:
        assert s.last_doctor_pass().srt_version == "0.0.80"


def _record_pass(world, **results):
    with world.store() as s:
        s.record_doctor_run(claude_version=world.children.versions[0], codex_version=world.children.versions[1],
                            srt_version=ch.SRT_VERSION, passed=True,
                            results={"suite": doctor.SUITE_VERSION, "deny_paths": list(doctor.deny_list(world.config)),
                                     "probes": [], **results})


def test_a_matching_pass_is_reused(world):
    _record_pass(world)
    assert world.run()[0] == rn.EXIT_READY
    assert world.children.doctor_calls == 0


def test_a_pass_recorded_under_built_in_defaults_reruns_doctor_once_gaffer_toml_exists(world):
    """Conservative extension: `gaffer doctor` before gaffer.toml proved a stand-in vault, not the real one."""
    _record_pass(world, deny_paths=list(doctor.deny_list(None)))
    code, out = world.run()
    assert code == rn.EXIT_READY and world.children.doctor_calls == 1
    assert "the deny list differs" in out
    world.run("ENG-2")
    assert world.children.doctor_calls == 1


def test_a_pass_from_an_older_probe_suite_reruns_doctor(world):
    _record_pass(world, suite=doctor.SUITE_VERSION - 1)
    code, out = world.run()
    assert world.children.doctor_calls == 1 and "the probe suite changed" in out


def test_the_report_records_the_deny_list_it_proved(rig, home):
    assert rig.run().to_dict()["deny_paths"][-1] == doctor.STAND_IN_VAULT
    cfg, _ = write_config(home)
    report = rig.run(cfg)
    assert report.deny_paths == doctor.deny_list(cfg) and report.deny_paths[-1] == "~/vault"
    assert "~/.claude.json" in report.deny_paths and report.to_dict()["suite"] == doctor.SUITE_VERSION


def test_a_failed_doctor_refuses_with_exit_4_naming_the_probe(world):
    world.children.doctor_failures = {"claude_file_read": "~/.claude.json: READ"}
    code, out = world.run()
    assert code == rn.EXIT_REFUSED
    assert "doctor failed (claude_file_read; R18)" in out and "~/.claude.json: READ" in out
    assert world.job() is None  # refused before any job, child or GitHub call
    with world.store() as s:
        assert s.last_doctor_pass() is None
        assert s.doctor_needed(*world.children.versions, srt_version=ch.SRT_VERSION)


def test_a_failed_doctor_reruns_next_time_and_a_pass_lets_the_run_proceed(world):
    world.children.doctor_failures = {"codex_web_search": "web_search events"}
    assert world.run()[0] == rn.EXIT_REFUSED
    world.children.doctor_failures = {}
    code, out = world.run()
    assert code == rn.EXIT_READY, out
    assert world.children.doctor_calls == 2


# --- the `gaffer doctor` command -----------------------------------------------------------------


def _fake_run(passed: bool):
    def run(cfg, *, home):
        probes = tuple(doctor.ProbeResult(n, passed or n != "srt_network", "ok" if passed or n != "srt_network" else "github.com: 200",
                                          "" if passed else "fix it") for n in doctor.PROBES)
        run.cfg = cfg
        return doctor.DoctorReport("2.1.308 (Claude Code)", "codex-cli 0.160.0", ch.SRT_VERSION, ch.SRT_VERSION,
                                   "test", probes)
    return run


def test_cli_exit_0_on_pass_and_records_it(tmp_path):
    home = tmp_path / "home"
    out = io.StringIO()
    run = _fake_run(True)
    assert rn.doctor_main(None, home=home, out=out, run=run) == rn.EXIT_READY
    assert run.cfg is None and "probing under built-in defaults" in out.getvalue()
    assert "gaffer doctor: passed (22 probes)" in out.getvalue()
    with st.Store(st.default_db_path(home)) as s:
        assert s.last_doctor_pass().claude_version == "2.1.308 (Claude Code)"


def test_cli_exit_4_on_failure_naming_the_probe(tmp_path):
    out = io.StringIO()
    assert rn.doctor_main(None, home=tmp_path / "home", out=out, run=_fake_run(False)) == rn.EXIT_REFUSED
    assert "FAIL  srt_network" in out.getvalue() and "FAILED 1 of 22 probes: srt_network" in out.getvalue()


def test_cli_uses_an_explicit_config(home, tmp_path):
    cfg, _ = write_config(home)
    run = _fake_run(True)
    assert rn.doctor_main(cfg.path, home=home, out=io.StringIO(), run=run) == rn.EXIT_READY
    assert run.cfg is not None and run.cfg.vault_path == home / "vault"


def test_cli_with_a_missing_explicit_config_is_refused(tmp_path):
    out = io.StringIO()
    assert rn.doctor_main(tmp_path / "nope.toml", home=tmp_path, out=out, run=_fake_run(True)) == rn.EXIT_REFUSED
    assert "not found" in out.getvalue()


def test_cli_refuses_while_a_run_holds_the_run_lock(tmp_path):
    home = tmp_path / "home"
    fd = rn._flock(rn.run_lock_path(home), "the run lock", "test")
    try:
        out = io.StringIO()
        assert rn.doctor_main(None, home=home, out=out, run=_fake_run(True)) == rn.EXIT_REFUSED
        assert "run lock" in out.getvalue()
    finally:
        import os
        os.close(fd)


def test_cli_parser_has_the_doctor_subcommand():
    args = rn.build_parser().parse_args(["doctor", "--config", "/tmp/g.toml"])
    assert args.command == "doctor" and args.config == Path("/tmp/g.toml")
