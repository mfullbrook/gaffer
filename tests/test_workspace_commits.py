"""Job clone (R21, R28, R30), verification worktree (R4, R29), writer gates
(R7, R10, R30), push (never force), PR opening (R12, R27) and the round
summary comment (R9). Disposable repos only; GitHub is a fake transport."""

import json
from pathlib import Path

import pytest

from gaffer import workspace as w
from gaffer.workspace import GateAction
from workspace_helpers import FakeGitHub, Job, client, fake_gitleaks, run_git, write

FIXTURES = Path(__file__).parent / "fixtures" / "coderabbit"


@pytest.fixture
def job(tmp_path):
    return Job(tmp_path)


@pytest.fixture
def tested(job):
    """A job with its tester commit in place."""
    job.add_tester_commit()
    return job


def gate(job, **kw):
    kw.setdefault("previous_head", job.ws.tester_commit)
    return w.check_writer_result(job.ws, job.repo, **kw)


# --- job clone -------------------------------------------------------------------


def test_clone_lives_under_job_root(job):
    assert job.ws.root == job.config.job_root / "ENG-123"
    assert job.clone == job.ws.root / "repo"
    assert (job.clone / ".git").is_dir()
    assert job.ws.tmp.is_dir()


def test_origin_is_the_remote_url_not_marks_checkout(job):
    assert run_git(job.clone, "remote", "get-url", "origin") == str(job.remote)


def test_default_origin_is_the_github_url(tmp_path):
    from workspace_helpers import make_config, make_source

    src = make_source(tmp_path)
    cfg = make_config(tmp_path, src)
    ws = w.create_workspace(cfg, cfg.repos["ENG"], "ENG-9", "gaffer/ENG-9", fetch_base=False)
    assert run_git(ws.clone, "remote", "get-url", "origin") == "https://github.com/mfullbrook/app.git"
    assert ws.base_sha == run_git(src, "rev-parse", "main")


def test_commit_identity_is_in_the_clone_config(job):
    assert run_git(job.clone, "config", "--local", "user.name") == w.GIT_USER_NAME
    assert run_git(job.clone, "config", "--local", "user.email") == w.GIT_USER_EMAIL


def test_branch_checked_out_at_recorded_base(job):
    assert run_git(job.clone, "symbolic-ref", "HEAD") == "refs/heads/gaffer/ENG-123"
    assert job.head() == job.ws.base_sha == run_git(job.remote, "rev-parse", "main")
    assert run_git(job.clone, "status", "--porcelain") == ""


def test_base_is_fetched_from_origin_not_the_stale_local_branch(tmp_path):
    from workspace_helpers import make_config, make_remote, make_source

    src = make_source(tmp_path)
    remote = make_remote(tmp_path, src)
    # Someone merged to main on GitHub; Mark's checkout hasn't pulled.
    other = tmp_path / "other"
    run_git(tmp_path, "clone", "-q", str(remote), str(other))
    write(other, "NEWS.md", "news\n")
    run_git(other, "add", "-A")
    run_git(other, "commit", "-q", "-m", "upstream change")
    run_git(other, "push", "-q", "origin", "main")
    cfg = make_config(tmp_path, src)
    ws = w.create_workspace(cfg, cfg.repos["ENG"], "ENG-1", "gaffer/ENG-1", remote_url=str(remote))
    assert ws.base_sha == run_git(remote, "rev-parse", "main") != run_git(src, "rev-parse", "main")


def test_marks_checkout_is_untouched(job):
    assert run_git(job.src, "status", "--porcelain") == ""
    assert run_git(job.src, "remote") == ""
    assert run_git(job.src, "branch", "--format=%(refname)") == "refs/heads/main"


def test_existing_job_dir_is_refused(job):
    with pytest.raises(w.WorkspaceError, match="already exists"):
        w.create_workspace(job.config, job.repo, "ENG-123", "gaffer/ENG-123", remote_url=str(job.remote))


@pytest.mark.parametrize("job_id", ["../x", "a/b", ".hidden", "", "x" * 101, "a..b"])
def test_bad_job_ids_are_refused(job, job_id):
    with pytest.raises(ValueError):
        w.job_dir(job.config, job_id)


@pytest.mark.parametrize("branch", ["main", "gaffer/bad name", "gaffer/x..y", "-gaffer/x"])
def test_bad_branch_names_are_refused(job, branch):
    with pytest.raises(ValueError):
        w.create_workspace(job.config, job.repo, "ENG-5", branch, remote_url=str(job.remote))


def test_branch_name():
    assert w.branch_name("ENG-123") == "gaffer/ENG-123"
    assert w.branch_name("ENG-123", 2) == "gaffer/ENG-123-2"
    with pytest.raises(ValueError):
        w.branch_name("eng-123")


def test_record_round_trip(tested):
    ws2 = w.Workspace.from_record(json.loads(json.dumps(tested.ws.to_record())))
    assert ws2.to_record() == tested.ws.to_record()
    assert ws2.tester_commit == tested.ws.tester_commit


# --- installs and verification -------------------------------------------------------


class RecordingInstaller:
    def __init__(self, exit_code=0):
        self.requests = []
        self.exit_code = exit_code

    def __call__(self, request):
        self.requests.append(request)
        return w.InstallResult(self.exit_code, "tail")


def test_verification_worktree_is_detached_at_exact_head_inside_job_dir(tested):
    head = tested.writer_commit({"src/app/__init__.py": "def add(a, b):\n    return a + b + 0\n"})
    v = w.prepare_verification(tested.ws, tested.repo, head)
    assert v.path == tested.ws.root / "verify"
    assert v.path.is_relative_to(tested.ws.root)
    assert run_git(v.path, "rev-parse", "HEAD") == head
    assert run_git(v.path, "symbolic-ref", "-q", "HEAD", check=False) == ""
    assert (v.path / "src/app/__init__.py").read_text().endswith("+ 0\n")


def test_verification_is_rebuilt_for_each_head(tested):
    first = tested.ws.tester_commit
    w.prepare_verification(tested.ws, tested.repo, first)
    (tested.ws.verify / "junk.txt").write_text("left over from a test run\n")
    head = tested.writer_commit({"README.md": "changed\n"})
    v = w.prepare_verification(tested.ws, tested.repo, head)
    assert run_git(v.path, "rev-parse", "HEAD") == head
    assert not (v.path / "junk.txt").exists()
    assert run_git(v.path, "status", "--porcelain") == ""


def test_verification_does_not_disturb_the_writers_clone(tested):
    (tested.clone / "scratch.txt").write_text("writer's uncommitted work\n")
    w.prepare_verification(tested.ws, tested.repo, tested.ws.tester_commit)
    assert (tested.clone / "scratch.txt").exists()


@pytest.mark.parametrize("head", ["HEAD", "main", "abc1234", "0" * 40])
def test_verification_needs_a_full_existing_sha(tested, head):
    with pytest.raises(w.WorkspaceError):
        w.prepare_verification(tested.ws, tested.repo, head)


def test_no_install_command_means_no_install(tested):
    inst = RecordingInstaller()
    v = w.prepare_verification(tested.ws, tested.repo, tested.ws.tester_commit, install=inst)
    assert inst.requests == [] and v.install is None


def test_php_verification_install_is_offline_from_the_job_cache(tmp_path):
    job = Job(tmp_path, ecosystems='["php"]', runner="phpunit", extra='install_command = "composer install --no-progress"\n')
    job.add_tester_commit()
    inst = RecordingInstaller()
    v = w.prepare_verification(job.ws, job.repo, job.ws.tester_commit, install=inst)
    (req,) = inst.requests
    assert req.cwd == job.ws.verify
    assert req.allowed_hosts == ()
    assert req.write_root == job.ws.root
    assert req.env["COMPOSER_HOME"] == str(job.ws.root / "composer-home")
    assert req.env["COMPOSER_CACHE_DIR"] == str(job.ws.root / "composer-home" / "cache")
    assert req.env["COMPOSER_NO_INTERACTION"] == "1"
    assert req.env["TMPDIR"] == str(job.ws.tmp)
    assert (job.ws.root / "composer-home" / "cache").is_dir()
    assert v.install.ok


def test_php_job_clone_install_warms_the_cache_with_php_hosts(tmp_path):
    job = Job(tmp_path, ecosystems='["php"]', runner="phpunit", extra='install_command = "composer install"\n')
    inst = RecordingInstaller()
    w.run_install(job.ws, job.repo, job.clone, inst)
    (req,) = inst.requests
    assert req.cwd == job.clone
    assert req.allowed_hosts == ("repo.packagist.org", "packagist.org", "api.github.com", "codeload.github.com")


def test_python_verification_install_uses_registry_hosts(tmp_path):
    job = Job(tmp_path, extra='install_command = "uv sync"\n')
    job.add_tester_commit()
    inst = RecordingInstaller(exit_code=1)
    v = w.prepare_verification(job.ws, job.repo, job.ws.tester_commit, install=inst)
    assert inst.requests[0].allowed_hosts == ("pypi.org", "files.pythonhosted.org")
    assert "COMPOSER_HOME" not in inst.requests[0].env
    assert not v.install.ok


def test_install_only_in_clone_or_verify(tmp_path):
    job = Job(tmp_path, extra='install_command = "uv sync"\n')
    with pytest.raises(ValueError):
        w.run_install(job.ws, job.repo, job.src, RecordingInstaller())


# --- writer gates -------------------------------------------------------------------


def test_clean_commit_passes(tested):
    head = tested.writer_commit({"src/app/feature.py": "x = 1\n"})
    r = gate(tested)
    assert r.action is GateAction.OK and r.ok
    assert r.head == head and r.new_commits == (head,)
    assert r.guarded_changes == ()


def test_dirty_tree_gets_one_nudge_then_blocks(tested):
    tested.writer_commit({"src/app/feature.py": "x = 1\n"})
    (tested.clone / "src/app/feature.py").write_text("x = 2\n")
    first = gate(tested)
    assert first.action is GateAction.NUDGE
    assert first.message == "commit or discard and return the result"
    assert first.dirty == ("src/app/feature.py",)
    second = gate(tested, nudged=True)
    assert second.action is GateAction.BLOCKED and second.reason == w.WRITER_DIRTY


def test_untracked_file_is_dirty(tested):
    tested.writer_commit({"src/app/feature.py": "x = 1\n"})
    (tested.clone / "notes.txt").write_text("todo\n")
    assert gate(tested).action is GateAction.NUDGE


def test_nudge_that_commits_passes(tested):
    tested.writer_commit({"src/app/feature.py": "x = 1\n"})
    (tested.clone / "src/app/more.py").write_text("y = 1\n")
    assert gate(tested).action is GateAction.NUDGE
    tested.writer_commit({}, "commit the rest")
    assert gate(tested, nudged=True).ok


def test_no_new_commit_blocks_writer_no_change(tested):
    r = gate(tested)
    assert r.reason == w.WRITER_NO_CHANGE


def test_all_declined_resume_without_commit_passes(tested):
    assert gate(tested, require_new_commit=False).ok


def test_cited_sha_without_new_commit_is_no_change(tested):
    r = gate(tested, require_new_commit=False, cited_shas=[tested.ws.tester_commit])
    assert r.reason == w.WRITER_NO_CHANGE


def test_tester_file_change_needs_trailer(tested):
    tested.writer_commit({"tests/test_feature.py": "def test_feature():\n    assert True\n"}, "make it pass")
    r = gate(tested)
    assert r.reason == w.TESTER_CHANGE_UNEXPLAINED
    assert "tests/test_feature.py" in r.message
    assert r.guarded_changes[0].kind == "tester" and r.guarded_changes[0].reason is None


def test_tester_file_change_with_trailer_passes_and_is_listed(tested):
    c = tested.writer_commit(
        {"tests/test_feature.py": "def test_feature():\n    assert 1\n"},
        "fix fixture\n\nTester-Change-Reason: the fixture used the wrong unit\n",
    )
    r = gate(tested)
    assert r.ok
    assert r.guarded_changes == (w.GuardedChange(c, "tests/test_feature.py", "tester", "the fixture used the wrong unit"),)


def test_tester_modified_existing_file_counts_as_tester_file(tmp_path):
    job = Job(tmp_path)
    job.add_tester_commit({"tests/test_app.py": "def test_add():\n    assert False\n"})
    job.writer_commit({"tests/test_app.py": "def test_add():\n    pass\n"})
    assert gate(job).reason == w.TESTER_CHANGE_UNEXPLAINED


def test_deleting_or_renaming_tester_file_needs_trailer(tested):
    run_git(tested.clone, "mv", "tests/test_feature.py", "tests/test_renamed.py")
    run_git(tested.clone, "commit", "-q", "-m", "rename")
    r = gate(tested)
    assert r.reason == w.TESTER_CHANGE_UNEXPLAINED


@pytest.mark.parametrize("path", ["pyproject.toml", "tests/conftest.py", "sub/Makefile", "uv.lock"])
def test_harness_change_needs_trailer(tested, path):
    tested.writer_commit({path: "changed = true\n"})
    r = gate(tested)
    assert r.reason == w.TESTER_CHANGE_UNEXPLAINED
    assert r.guarded_changes[0].kind == "harness"


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".github/actions/setup/action.yml"])
def test_ci_files_are_harness_with_a_reason_and_flagged(tested, path):
    tested.writer_commit({path: "name: x\n"})
    assert gate(tested).reason == w.TESTER_CHANGE_UNEXPLAINED
    run_git(tested.clone, "commit", "-q", "--amend", "-m", "ci\n\nTester-Change-Reason: new job step\n")
    r = gate(tested)
    assert r.ok
    assert r.guarded_changes[0].kind == "ci" and r.guarded_changes[0].reason == "new job step"


def test_trailer_must_be_on_the_commit_that_changes_the_file(tested):
    tested.writer_commit({"pyproject.toml": "x = 1\n"}, "deps")
    tested.writer_commit({"src/app/a.py": "a = 1\n"}, "code\n\nTester-Change-Reason: unrelated\n")
    assert gate(tested).reason == w.TESTER_CHANGE_UNEXPLAINED


def test_empty_trailer_is_unexplained(tested):
    tested.writer_commit({"pyproject.toml": "x = 1\n"}, "deps\n\nTester-Change-Reason:   \n")
    assert gate(tested).reason == w.TESTER_CHANGE_UNEXPLAINED


@pytest.mark.parametrize("path", [".claude/settings.json", ".codex/config.toml", ".mcp.json", "sub/.claude/x.md"])
def test_forbidden_paths_block_even_with_trailer(tested, path):
    tested.writer_commit({path: "{}\n"}, "cfg\n\nTester-Change-Reason: please\n")
    r = gate(tested)
    assert r.reason == w.FORBIDDEN_PATH and path in r.message


def test_repo_forbidden_paths_extend_the_defaults(tmp_path):
    job = Job(tmp_path, extra='forbidden_paths = ["deploy/**"]\n')
    job.add_tester_commit()
    job.writer_commit({"deploy/prod.sh": "echo\n"})
    assert gate(job).reason == w.FORBIDDEN_PATH


def test_cited_shas_must_be_new_commits(tested):
    a = tested.writer_commit({"src/app/a.py": "a = 1\n"})
    b = tested.writer_commit({"src/app/b.py": "b = 1\n"})
    r = gate(tested, cited_shas=[a, b[:9]])
    assert r.ok
    assert r.cited == {a: a, b[:9]: b}


@pytest.mark.parametrize("cite", ["previous", "base", "missing", "garbage", "other-branch", "HEAD"])
def test_bad_cited_sha_is_writer_no_result(tested, cite):
    tested.writer_commit({"src/app/a.py": "a = 1\n"})
    run_git(tested.clone, "branch", "side", tested.ws.base_sha)
    run_git(tested.clone, "switch", "-q", "side")
    side = tested.writer_commit({"src/app/side.py": "s = 1\n"})
    run_git(tested.clone, "switch", "-q", "gaffer/ENG-123")
    value = {
        "previous": tested.ws.tester_commit,
        "base": tested.ws.base_sha,
        "missing": "f" * 40,
        "garbage": "not-a-sha; rm -rf /",
        "other-branch": side,
        "HEAD": "HEAD",
    }[cite]
    r = gate(tested, cited_shas=[value])
    assert r.reason == w.WRITER_NO_RESULT


def test_cited_shas_after_a_resume_are_relative_to_the_packet_head(tested):
    packet_head = tested.writer_commit({"src/app/a.py": "a = 1\n"})
    fix = tested.writer_commit({"src/app/a.py": "a = 2\n"})
    assert gate(tested, previous_head=packet_head, cited_shas=[fix]).ok
    assert gate(tested, previous_head=packet_head, cited_shas=[packet_head]).reason == w.WRITER_NO_RESULT


def test_rewritten_tester_commit_blocks(tested):
    run_git(tested.clone, "reset", "-q", "--hard", tested.ws.base_sha)
    tested.writer_commit({"tests/test_feature.py": "def test_feature():\n    pass\n"})
    r = gate(tested)
    assert r.reason == w.TESTER_COMMIT_REWRITTEN


def test_rewritten_pushed_history_blocks(tested):
    pushed = tested.writer_commit({"src/app/a.py": "a = 1\n"})
    run_git(tested.clone, "commit", "-q", "--amend", "-m", "reworded")
    r = gate(tested, previous_head=pushed)
    assert r.reason == w.WRITER_HISTORY_REWRITTEN


def test_writer_switching_branch_blocks(tested):
    tested.writer_commit({"src/app/a.py": "a = 1\n"})
    run_git(tested.clone, "switch", "-q", "--detach")
    assert gate(tested).reason == w.WRITER_BRANCH_MOVED


def test_gate_needs_a_tester_commit(job):
    with pytest.raises(w.WorkspaceError):
        gate(job, previous_head=job.ws.base_sha)


# --- push -----------------------------------------------------------------------------


@pytest.fixture
def scanner(tmp_path):
    return w.Gitleaks(fake_gitleaks(tmp_path))


def test_push_fast_forwards_exact_head(tested, scanner):
    head = tested.writer_commit({"src/app/a.py": "a = 1\n"})
    r = w.push(tested.ws, head, scanner=scanner, expected_remote=None)
    assert r.pushed and r.remote_before is None
    assert run_git(tested.remote, "rev-parse", "refs/heads/gaffer/ENG-123") == head
    head2 = tested.writer_commit({"src/app/b.py": "b = 1\n"})
    r2 = w.push(tested.ws, head2, scanner=scanner, expected_remote=head)
    assert r2.pushed and r2.remote_before == head
    assert w.remote_head(tested.ws) == head2


def test_push_is_idempotent(tested, scanner):
    head = tested.writer_commit({"src/app/a.py": "a = 1\n"})
    w.push(tested.ws, head, scanner=scanner, expected_remote=None)
    again = w.push(tested.ws, head, scanner=scanner, expected_remote=None)
    assert not again.pushed


def test_push_never_forces_over_a_foreign_head(tested, scanner):
    head = tested.writer_commit({"src/app/a.py": "a = 1\n"})
    w.push(tested.ws, head, scanner=scanner, expected_remote=None)
    # A human pushes to the branch on "GitHub".
    other = tested.tmp / "human"
    run_git(tested.tmp, "clone", "-q", "-b", "gaffer/ENG-123", str(tested.remote), str(other))
    write(other, "human.txt", "hi\n")
    run_git(other, "add", "-A")
    run_git(other, "commit", "-q", "-m", "human fix")
    run_git(other, "push", "-q", "origin", "gaffer/ENG-123")
    human = run_git(other, "rev-parse", "HEAD")
    head2 = tested.writer_commit({"src/app/b.py": "b = 1\n"})
    with pytest.raises(w.PushRejected) as e:
        w.push(tested.ws, head2, scanner=scanner, expected_remote=head)
    assert e.value.reason == w.FOREIGN_HEAD
    assert w.remote_head(tested.ws) == human


def test_push_rejected_by_remote_is_foreign_head(tested, scanner):
    head = tested.writer_commit({"src/app/a.py": "a = 1\n"})
    w.push(tested.ws, head, scanner=scanner, expected_remote=None)
    run_git(tested.clone, "reset", "-q", "--hard", tested.ws.tester_commit)
    rewritten = tested.writer_commit({"src/app/a.py": "a = 2\n"})
    # Even if the caller claims the remote is where it is, git refuses a non-fast-forward.
    with pytest.raises(w.PushRejected):
        w.push(tested.ws, rewritten, scanner=scanner, expected_remote=head)
    assert w.remote_head(tested.ws) == head


def test_push_requires_tester_commit_in_history(tested, scanner):
    run_git(tested.clone, "reset", "-q", "--hard", tested.ws.base_sha)
    head = tested.writer_commit({"src/app/a.py": "a = 1\n"})
    with pytest.raises(w.WorkspaceError) as e:
        w.push(tested.ws, head, scanner=scanner, expected_remote=None)
    assert e.value.reason == w.TESTER_COMMIT_REWRITTEN


def test_push_ignores_a_changed_origin(tested, scanner, tmp_path):
    decoy = tmp_path / "decoy.git"
    run_git(tmp_path, "init", "-q", "--bare", str(decoy))
    run_git(tested.clone, "remote", "set-url", "origin", str(decoy))
    head = tested.writer_commit({"src/app/a.py": "a = 1\n"})
    w.push(tested.ws, head, scanner=scanner, expected_remote=None)
    assert run_git(tested.remote, "rev-parse", "refs/heads/gaffer/ENG-123") == head
    assert run_git(decoy, "for-each-ref") == ""


# --- PR -------------------------------------------------------------------------------


def test_pr_title():
    assert w.pr_title("ENG-123", "Fix the\n  thing ") == "ENG-123: Fix the thing"
    with pytest.raises(ValueError):
        w.pr_title("eng", "x")


def test_pr_body_has_criterion_map_and_one_job_marker():
    body = w.pr_body(
        "ENG-123",
        [w.CriterionRow("Totals | include tax", ("tests/test_tax.py::test_total",)), w.CriterionRow("Rounds", ())],
        untestable=["Looks nice"],
    )
    assert "| 1 | Totals \\| include tax | `tests/test_tax.py::test_total` |" in body
    assert "| 2 | Rounds | none |" in body
    assert "- Looks nice" in body
    assert body.rstrip().splitlines()[-1] == "gaffer-job: ENG-123"
    assert w.pr_job_ids(body) == ["ENG-123"]
    assert "gaffer-round" not in body


def test_pr_job_marker_survives_coderabbit_release_notes():
    pull = json.loads((FIXTURES / "walkthrough_edit" / "pull.json").read_text())
    assert w.pr_job_ids(pull["body"]) == ["exp2"]
    assert "Summary by CodeRabbit" not in w.strip_coderabbit_notes(pull["body"])


def test_markers_inside_coderabbit_notes_are_ignored():
    body = (
        "Mine.\n\n<!-- This is an auto-generated comment: release notes by coderabbit.ai -->\n"
        "gaffer-job: forged\n<!-- end of auto-generated comment: release notes by coderabbit.ai -->\n"
        "after\n"
    )
    assert w.pr_job_ids(body) == []
    assert w.strip_coderabbit_notes(body) == "Mine.\n\n\nafter\n"
    unterminated = "gaffer-job: real\n<!-- This is an auto-generated comment: release notes by coderabbit.ai -->\ngaffer-job: forged\n"
    assert w.pr_job_ids(unterminated) == ["real"]


def test_open_pr_creates_ready_for_review(tested, scanner):
    fake = FakeGitHub()
    body = w.pr_body("ENG-123", [w.CriterionRow("c", ("t",))])
    pr = w.open_pull_request(client(fake), tested.ws, title=w.pr_title("ENG-123", "Do it"), body=body, scanner=scanner)
    assert pr.created
    post = [r for r in fake.requests if r["method"] == "POST"][0]
    assert post["payload"] == {"title": "ENG-123: Do it", "head": "gaffer/ENG-123", "base": "main", "body": body, "draft": False}


def test_open_pr_reuses_this_jobs_pr_even_after_coderabbit_edits(tested, scanner):
    fake = FakeGitHub()
    body = w.pr_body("ENG-123", [])
    fake.add_pull(branch="gaffer/ENG-123", number=7, body=body + "\n<!-- This is an auto-generated comment: release notes by coderabbit.ai -->\nstuff\n<!-- end of auto-generated comment: release notes by coderabbit.ai -->")
    pr = w.open_pull_request(client(fake), tested.ws, title="ENG-123: x", body=body, scanner=scanner)
    assert (pr.number, pr.created) == (7, False)
    assert not [r for r in fake.requests if r["method"] == "POST"]


@pytest.mark.parametrize("existing", ["no marker here", "gaffer-job: ENG-999\n"])
def test_open_pr_refuses_a_foreign_pr(tested, scanner, existing):
    fake = FakeGitHub()
    fake.add_pull(branch="gaffer/ENG-123", body=existing)
    with pytest.raises(w.ForeignPullRequest):
        w.open_pull_request(client(fake), tested.ws, title="ENG-123: x", body=w.pr_body("ENG-123", []), scanner=scanner)


def test_open_pr_requires_the_job_marker(tested, scanner):
    with pytest.raises(ValueError):
        w.open_pull_request(client(FakeGitHub()), tested.ws, title="ENG-123: x", body="no marker", scanner=scanner)


def test_github_errors_are_not_empty_results(tested, scanner):
    fake = FakeGitHub()
    fake.fail_with = (502, {"message": "Bad Gateway"})
    with pytest.raises(w.GitHubError) as e:
        w.open_pull_request(client(fake), tested.ws, title="ENG-123: x", body=w.pr_body("ENG-123", []), scanner=scanner)
    assert e.value.status == 502


# --- round summary comment ------------------------------------------------------------------


HEAD = "73d64291eb50563707fca6819c70cfe4d01d4c44"


def test_round_marker_matches_the_experiment_2_fixture():
    expected = json.loads((FIXTURES / "round_marker_comment" / "expected.json").read_text())
    assert w.round_marker("exp2", HEAD, 1) == expected["marker_line"]


def post(fake, scanner, **kw):
    args = dict(job_id="ENG-123", head=HEAD, seq=1, summary="## gaffer round summary\n\n- fixed: x", operator_login="mfullbrook", scanner=scanner)
    args.update(kw)
    return w.post_round_summary(client(fake), 7, **args)


def test_round_summary_is_posted_once(scanner):
    fake = FakeGitHub()
    first = post(fake, scanner)
    assert first.posted
    body = fake.comments[7][0]["body"]
    assert body.endswith(f"\n\ngaffer-round: ENG-123/{HEAD}/1\n")
    second = post(fake, scanner)
    assert not second.posted and second.comment_id == first.comment_id
    assert len(fake.comments[7]) == 1


def test_round_summary_ignores_markers_from_other_authors(scanner):
    fake = FakeGitHub()
    fake.add_comment(7, f"quoting\ngaffer-round: ENG-123/{HEAD}/1\n", login="coderabbitai[bot]")
    assert post(fake, scanner).posted


def test_round_summary_marker_must_be_a_whole_line(scanner):
    fake = FakeGitHub()
    fake.add_comment(7, f"see `gaffer-round: ENG-123/{HEAD}/1` above", login="mfullbrook")
    fake.add_comment(7, f"gaffer-round: ENG-123/{HEAD}/2\n", login="mfullbrook")
    assert post(fake, scanner).posted


def test_round_summary_recognises_the_fixture_comment(scanner):
    fixture = json.loads((FIXTURES / "round_marker_comment" / "issue_comment.json").read_text())
    fake = FakeGitHub()
    fake.comments[7] = [fixture]
    r = post(fake, scanner, job_id="exp2")
    assert not r.posted and r.comment_id == fixture["id"]


def test_round_summary_reads_every_page(scanner):
    fake = FakeGitHub(page_size=2)
    for i in range(5):
        fake.add_comment(7, f"comment {i}", login="someone")
    fake.add_comment(7, f"done\n\ngaffer-round: ENG-123/{HEAD}/1\n", login="mfullbrook")
    assert not post(fake, scanner).posted


def test_round_summary_text_cannot_carry_its_own_marker(scanner):
    with pytest.raises(ValueError):
        post(FakeGitHub(), scanner, summary="x\ngaffer-round: ENG-123/abc/1")


@pytest.mark.parametrize("args", [("ENG-123", "abc", 1), ("ENG-123", HEAD, 0), ("../x", HEAD, 1)])
def test_round_marker_validates(args):
    with pytest.raises(ValueError):
        w.round_marker(*args)
