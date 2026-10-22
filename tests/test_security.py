"""SC8 for the workspace: gitleaks over push diffs, comments and packet text
(R15) with findings redacted; the GitHub token never logged; nothing the
writer plants in the job clone runs as gaffer (R21 trust boundary).

The fake gitleaks exercises the integration logic. Tests that need the real
binary skip when it isn't installed."""

import base64
import json
import logging
import shutil
import urllib.request

import pytest

from gaffer import workspace as w
from gaffer.config import Secret
from workspace_helpers import FAKE_TOKEN, FakeGitHub, Job, client, fake_gitleaks, fake_secret, run_git, write

needs_gitleaks = pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed (brew install gitleaks)")


@pytest.fixture
def tested(tmp_path):
    job = Job(tmp_path)
    job.add_tester_commit()
    return job


@pytest.fixture
def scanner(tmp_path):
    return w.Gitleaks(fake_gitleaks(tmp_path))


# --- gitleaks integration -----------------------------------------------------------


def test_clean_text_passes(scanner):
    r = w.scan_texts(scanner, [("comment", "all good\n")], source="comment")
    assert r.clean and r.summary() == "no secrets found"
    assert r.require_clean() is r


def test_secret_in_comment_is_found_and_redacted(scanner):
    secret = fake_secret("B")
    r = w.scan_texts(scanner, [("a", "fine\n"), ("b", f"line one\nkey={secret}\n")], source="comment")
    assert not r.clean
    (f,) = r.findings
    assert f.rule_id == "fake-rule" and f.location == "b line 2" and f.source == "comment"
    # The fake gitleaks returns Secret and Match unredacted; gaffer never surfaces them.
    for text in (str(f), r.summary(), repr(r)):
        assert secret not in text
    with pytest.raises(w.SecretDetected) as e:
        r.require_clean()
    assert e.value.reason == w.SECRET_DETECTED
    assert secret not in str(e.value)


def test_packet_items_are_scanned_with_labels(scanner):
    items = [(f"item {i}", f"finding {i}\n") for i in range(5)] + [("item 5", f"log tail\n{fake_secret('C')}\n")]
    r = w.scan_texts(scanner, items, source="packet item")
    assert [f.location for f in r.findings] == ["item 5 line 2"]


def test_gitleaks_runs_isolated_with_redaction_flags(tmp_path):
    log = tmp_path / "gitleaks.log"
    scanner = w.Gitleaks(fake_gitleaks(tmp_path, log=log))
    w.scan_texts(scanner, [("x", "hello\n")], source="comment")
    call = json.loads(log.read_text().splitlines()[0])
    assert call["argv"][0] == "stdin"
    for flag in ("--redact", "--ignore-gitleaks-allow", "--no-banner"):
        assert flag in call["argv"]
    assert call["argv"][call["argv"].index("--report-format") + 1] == "json"
    # An empty working dir: no .gitleaksignore or .gitleaks.toml can apply.
    assert call["files"] == []
    assert not any(k.startswith(("GITLEAKS", "GITHUB", "GH_")) for k in call["env"])


def test_scanner_crash_fails_closed(tmp_path):
    scanner = w.Gitleaks(fake_gitleaks(tmp_path, mode="crash"))
    with pytest.raises(w.SecretScanError) as e:
        w.scan_texts(scanner, [("x", "text\n")], source="comment")
    assert e.value.reason == w.SECRET_SCAN_FAILED
    assert "boom" not in str(e.value)


@pytest.mark.parametrize("mode", ["no-report", "garbage"])
def test_unreadable_report_with_leaks_fails_closed(tmp_path, mode):
    scanner = w.Gitleaks(fake_gitleaks(tmp_path, mode=mode))
    with pytest.raises(w.SecretScanError):
        w.scan_texts(scanner, [("x", fake_secret())], source="comment")


def test_missing_gitleaks_fails_closed(tmp_path):
    with pytest.raises(w.SecretScanError, match="not installed"):
        w.Gitleaks(tmp_path / "nope").scan("text")


# --- push gate ---------------------------------------------------------------------


def test_secret_in_push_diff_blocks_and_nothing_is_pushed(tested, scanner):
    secret = fake_secret("D")
    head = tested.writer_commit({"src/app/config.py": f"TOKEN = '{secret}'\n"})
    with pytest.raises(w.SecretDetected) as e:
        w.push(tested.ws, head, scanner=scanner, expected_remote=None)
    (f,) = e.value.result.findings
    assert f.source == "push diff"
    assert f.location == f"commit {head[:12]} src/app/config.py"
    assert secret not in str(e.value)
    assert w.remote_head(tested.ws) is None


def test_secret_added_then_removed_is_still_caught(tested, scanner):
    added = tested.writer_commit({"src/app/config.py": f"TOKEN = '{fake_secret('E')}'\n"})
    head = tested.writer_commit({"src/app/config.py": "TOKEN = None\n"})
    assert run_git(tested.clone, "diff", tested.ws.base_sha, head).count("FAKESECRET") == 0
    r = w.scan_push(tested.ws, head, scanner)
    assert [f.location for f in r.findings] == [f"commit {added[:12]} src/app/config.py"]


def test_secret_already_on_the_base_does_not_block(tmp_path, scanner):
    from workspace_helpers import make_config, make_remote, make_source

    src = make_source(tmp_path)
    write(src, "src/app/legacy.py", f"OLD = '{fake_secret('L')}'\nx = 1\n")
    run_git(src, "add", "-A")
    run_git(src, "commit", "-q", "-m", "legacy")
    remote = make_remote(tmp_path, src)
    cfg = make_config(tmp_path, src)
    ws = w.create_workspace(cfg, cfg.repos["ENG"], "ENG-2", "gaffer/ENG-2", remote_url=str(remote))
    written = w.write_tester_files(ws, cfg.repos["ENG"], [w.TesterFile("tests/test_l.py", "def test_l():\n    assert 0\n")])
    w.commit_tester_files(ws, written, "tester")
    write(ws.clone, "src/app/legacy.py", f"OLD = '{fake_secret('L')}'\nx = 2\n")
    run_git(ws.clone, "commit", "-q", "-am", "edit near it")
    assert w.scan_push(ws, run_git(ws.clone, "rev-parse", "HEAD"), scanner).clean


def test_secret_in_commit_message_is_caught(tested, scanner):
    head = tested.writer_commit({"src/app/a.py": "a = 1\n"}, f"debug with {fake_secret('F')}")
    (f,) = w.scan_push(tested.ws, head, scanner).findings
    assert f.location == f"commit {head[:12]} (message)"


def test_secret_in_tester_files_is_caught(tmp_path, scanner):
    job = Job(tmp_path)
    job.add_tester_commit({"tests/test_x.py": f"KEY = '{fake_secret('G')}'\n"})
    head = job.writer_commit({"src/app/a.py": "a = 1\n"})
    assert len(w.scan_push(job.ws, head, scanner).findings) == 1


def test_secret_in_round_comment_is_not_posted(scanner):
    fake = FakeGitHub()
    with pytest.raises(w.SecretDetected):
        w.post_round_summary(
            client(fake), 7, job_id="ENG-123", head="a" * 40, seq=1,
            summary=f"log: {fake_secret('H')}", operator_login="mfullbrook", scanner=scanner,
        )
    assert not [r for r in fake.requests if r["method"] == "POST"]


def test_secret_in_pr_text_blocks_pr_creation(tested, scanner):
    fake = FakeGitHub()
    body = w.pr_body("ENG-123", [w.CriterionRow(f"use {fake_secret('J')}", ())])
    with pytest.raises(w.SecretDetected):
        w.open_pull_request(client(fake), tested.ws, title="ENG-123: x", body=body, scanner=scanner)
    assert not [r for r in fake.requests if r["method"] == "POST"]


def test_writer_planted_gitleaks_config_does_not_apply(tested, tmp_path):
    log = tmp_path / "gl.log"
    scanner = w.Gitleaks(fake_gitleaks(tmp_path, log=log))
    write(tested.clone, ".gitleaksignore", "*\n")
    head = tested.writer_commit({".gitleaks.toml": "[allowlist]\npaths = ['.*']\n", "src/a.py": f"k = '{fake_secret('K')}'\n"})
    assert not w.scan_push(tested.ws, head, scanner).clean
    call = json.loads(log.read_text().splitlines()[0])
    assert call["cwd"] != str(tested.clone)


# --- real gitleaks -------------------------------------------------------------------


def _github_pat() -> str:
    # Assembled at runtime so no token-shaped literal is committed.
    return "ghp_" + "".join("aZ3kQ9mX7pL2wR8tY5nB1vC6"[i % 24] for i in range(36))


@needs_gitleaks
def test_real_gitleaks_finds_and_redacts():
    secret = _github_pat()
    r = w.scan_texts(w.Gitleaks(), [("comment", f"here: {secret}\n")], source="comment")
    assert not r.clean
    assert secret not in r.summary()


@needs_gitleaks
def test_real_gitleaks_passes_clean_text():
    assert w.scan_texts(w.Gitleaks(), [("comment", "nothing to see\n")], source="comment").clean


@needs_gitleaks
def test_real_gitleaks_ignores_allow_comments():
    secret = _github_pat()
    r = w.scan_texts(w.Gitleaks(), [("diff", f"+token = '{secret}'  # gitleaks:allow\n")], source="push diff")
    assert not r.clean


# --- token handling -------------------------------------------------------------------


def test_token_never_logged_or_shown(caplog, tested, scanner):
    caplog.set_level(logging.DEBUG)
    fake = FakeGitHub()
    gh = client(fake)
    assert FAKE_TOKEN not in repr(gh)
    head = tested.writer_commit({"src/app/a.py": "a = 1\n"})
    w.push(tested.ws, head, scanner=scanner, expected_remote=None, token=Secret(FAKE_TOKEN))
    pr = w.open_pull_request(gh, tested.ws, title="ENG-123: x", body=w.pr_body("ENG-123", []), scanner=scanner)
    w.post_round_summary(gh, pr.number, job_id="ENG-123", head=head, seq=1, summary="s", operator_login="mfullbrook", scanner=scanner)
    assert fake.requests and all(r["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}" for r in fake.requests)
    assert caplog.records
    assert FAKE_TOKEN not in caplog.text


def test_github_error_echoing_the_token_is_scrubbed():
    fake = FakeGitHub()
    fake.fail_with = (401, {"message": f"bad credentials {FAKE_TOKEN}"})
    with pytest.raises(w.GitHubError) as e:
        client(fake).authenticated_login()
    assert FAKE_TOKEN not in str(e.value) and "<redacted>" in str(e.value)


def test_transport_failure_is_scrubbed():
    def boom(*args):
        raise OSError(f"connect failed for {FAKE_TOKEN}")

    with pytest.raises(w.GitHubError) as e:
        w.GitHubClient("mfullbrook/app", Secret(FAKE_TOKEN), transport=boom).authenticated_login()
    assert FAKE_TOKEN not in str(e.value)


def test_client_needs_a_secret_and_stays_on_the_api_host():
    with pytest.raises(TypeError):
        w.GitHubClient("mfullbrook/app", FAKE_TOKEN)
    gh = client(FakeGitHub())
    with pytest.raises(w.GitHubError, match="off the API host"):
        gh.request("GET", "https://evil.example/repos")


def test_pagination_link_off_the_api_host_is_refused():
    def transport(method, url, headers, body, timeout):
        return w.HttpResponse(200, {"Link": '<https://evil.example/next>; rel="next"'}, b"[]")

    gh = w.GitHubClient("mfullbrook/app", Secret(FAKE_TOKEN), transport=transport)
    with pytest.raises(w.GitHubError):
        gh.issue_comments(1)


def test_urllib_transport_keeps_auth_off_redirects(monkeypatch):
    seen = {}

    class Resp:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout):
        seen["req"] = req
        return Resp()

    monkeypatch.setattr(w._OPENER, "open", fake_open)
    w.urllib_transport("GET", "https://api.github.com/user", {"Authorization": "Bearer x", "Accept": "a"}, None, 5)
    req: urllib.request.Request = seen["req"]
    assert "Authorization" in req.unredirected_hdrs
    assert "Authorization" not in req.headers


def test_git_push_auth_goes_in_env_not_argv(monkeypatch, tested):
    calls = []
    real_run = w.subprocess.run

    def spy(argv, **kw):
        calls.append((argv, kw.get("env") or {}))
        return real_run(argv, **kw)

    monkeypatch.setattr(w.subprocess, "run", spy)
    tested.ws.remote_url = "https://github.com/mfullbrook/app.git"
    # ls-remote to the real host would be network: stop at the first git call.
    monkeypatch.setattr(w, "git", lambda *a, **kw: (_ for _ in ()).throw(w.GitError("stop")))
    env, secrets = w._auth_env(tested.ws.remote_url, Secret(FAKE_TOKEN))
    basic = base64.b64encode(f"x-access-token:{FAKE_TOKEN}".encode()).decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {basic}"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert set(secrets) == {FAKE_TOKEN, basic}
    assert w._auth_env("/local/path.git", Secret(FAKE_TOKEN)) == ({}, ())
    assert w._auth_env("https://evil.example/x.git", Secret(FAKE_TOKEN)) == ({}, ())
    with pytest.raises(w.GitError):
        w.remote_head(tested.ws, token=Secret(FAKE_TOKEN))


def test_git_errors_scrub_secrets(tested):
    with pytest.raises(w.GitError) as e:
        w.git(tested.clone, "rev-parse", "--verify", FAKE_TOKEN, secrets=[FAKE_TOKEN])
    assert FAKE_TOKEN not in str(e.value)


# --- the writer-controlled clone can't run code as gaffer ---------------------------------------


HOOKS = ("pre-commit", "commit-msg", "post-commit", "post-checkout", "pre-push", "reference-transaction", "pre-auto-gc")


def plant_hooks(clone, canary):
    hooks = clone / ".git" / "hooks"
    for name in HOOKS:
        (hooks / name).write_text(f"#!/bin/sh\necho {name} >> '{canary}'\n")
        (hooks / name).chmod(0o755)


def test_hooks_planted_by_the_writer_never_run(tmp_path):
    job = Job(tmp_path)
    canary = tmp_path / "canary"
    plant_hooks(job.clone, canary)
    job.add_tester_commit()
    # The writer's own commit would run its hooks inside its sandbox; turn them off here.
    write(job.clone, "src/app/a.py", "a = 1\n")
    run_git(job.clone, "-c", "core.hooksPath=/dev/null", "add", "-A")
    run_git(job.clone, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "impl")
    head = job.head()
    scanner = w.Gitleaks(fake_gitleaks(tmp_path))
    assert w.check_writer_result(job.ws, job.repo, previous_head=job.ws.tester_commit).ok
    w.prepare_verification(job.ws, job.repo, head)
    w.push(job.ws, head, scanner=scanner, expected_remote=None)
    assert not canary.exists()


def test_writer_config_changes_are_reverted_before_gaffer_runs_git(tmp_path):
    job = Job(tmp_path)
    canary = tmp_path / "canary"
    hookdir = tmp_path / "evil-hooks"
    hookdir.mkdir()
    for name in HOOKS:
        (hookdir / name).write_text(f"#!/bin/sh\necho {name} >> '{canary}'\n")
        (hookdir / name).chmod(0o755)
    cfg = job.clone / ".git" / "config"
    original = cfg.read_text()
    # A clean filter runs on `git add` and `git status`; fsmonitor on status.
    write(job.clone, "tests/.gitattributes", "* filter=evil\n")
    cfg.write_text(original + textwrap_config(hookdir, canary))
    files = [w.TesterFile("tests/test_feature.py", "def test_feature():\n    assert False\n")]
    written = w.write_tester_files(job.ws, job.repo, files)
    assert cfg.read_text() == original
    assert job.ws.config_restores
    run_git(job.clone, "add", "tests/.gitattributes")  # the filter now has no definition
    run_git(job.clone, "commit", "-q", "-m", "attrs")
    job.ws.base_sha = job.head()
    w.commit_tester_files(job.ws, written, "tester")
    cfg.write_text(original + textwrap_config(hookdir, canary))
    head = job.head()
    w.check_writer_result(job.ws, job.repo, previous_head=job.ws.tester_commit, require_new_commit=False)
    w.prepare_verification(job.ws, job.repo, head)
    assert not canary.exists()


def textwrap_config(hookdir, canary):
    return (
        f"[core]\n\thooksPath = {hookdir}\n\tfsmonitor = touch '{canary}'\n"
        f"[filter \"evil\"]\n\tclean = {evil_script(canary.parent, canary)}\n\tsmudge = cat\n"
        f"[remote \"origin\"]\n\turl = https://evil.example/x.git\n"
        f"[include]\n\tpath = {canary}.inc\n"
    )


def evil_script(where, canary):
    script = where / "evil-filter.sh"
    script.write_text(f"#!/bin/sh\necho ran >> '{canary}'\ncat\n")
    script.chmod(0o755)
    return script


def test_replaced_git_dir_is_refused(tested, tmp_path):
    other = tmp_path / "other"
    run_git(tmp_path, "init", "-q", str(other))
    shutil.rmtree(tested.clone / ".git")
    (tested.clone / ".git").write_text(f"gitdir: {other / '.git'}\n")
    with pytest.raises(w.WorkspaceTampered) as e:
        w.check_writer_result(tested.ws, tested.repo, previous_head=tested.ws.tester_commit)
    assert e.value.reason == w.WORKSPACE_TAMPERED


def test_symlinked_git_dir_is_refused(tested, tmp_path):
    moved = tmp_path / "moved-git"
    shutil.move(tested.clone / ".git", moved)
    (tested.clone / ".git").symlink_to(moved)
    with pytest.raises(w.WorkspaceTampered):
        w.prepare_verification(tested.ws, tested.repo, tested.ws.tester_commit)


def test_marks_global_git_config_is_not_used(tmp_path, monkeypatch):
    job = Job(tmp_path)
    canary = tmp_path / "global-canary"
    # A clean filter defined in global config would run on gaffer's `git add`.
    write(job.clone, "tests/.gitattributes", "*.py filter=evil\n")
    run_git(job.clone, "add", "-A")
    run_git(job.clone, "commit", "-q", "-m", "attrs")
    job.ws.base_sha = job.head()
    gconf = tmp_path / "gitconfig"
    gconf.write_text(f"[filter \"evil\"]\n\tclean = {evil_script(tmp_path, canary)}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gconf))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".gitconfig").write_text(gconf.read_text())
    job.add_tester_commit()
    assert not canary.exists()
