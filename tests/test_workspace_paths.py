"""R6: tester file paths are confined to test_dirs; realpath, symlink, '..',
count and size rules; existing tests may be edited and are listed."""

import os

import pytest

from gaffer import workspace as w
from gaffer.workspace import (
    MAX_TESTER_FILE_BYTES,
    MAX_TESTER_FILES,
    TESTER_INVALID,
    WorkspaceError,
    commit_tester_files,
    write_tester_files,
)
from workspace_helpers import Job, run_git


@pytest.fixture
def job(tmp_path):
    return Job(tmp_path)


def write_files(job, files):
    return write_tester_files(job.ws, job.repo, [w.TesterFile(p, c) for p, c in files.items()])


def rejected(job, files) -> w.TesterOutputInvalid:
    with pytest.raises(w.TesterOutputInvalid) as e:
        write_files(job, files)
    assert e.value.reason == TESTER_INVALID
    return e.value


def test_new_file_in_test_dir_is_created(job):
    result = write_files(job, {"tests/test_new.py": "def test_x():\n    assert False\n"})
    assert result.created == ("tests/test_new.py",)
    assert result.modified == ()
    assert (job.clone / "tests/test_new.py").read_text() == "def test_x():\n    assert False\n"


def test_nested_dirs_are_created(job):
    write_files(job, {"tests/unit/deep/test_new.py": "x = 1\n"})
    assert (job.clone / "tests/unit/deep/test_new.py").is_file()


def test_existing_test_file_may_be_edited_and_is_listed(job):
    result = write_files(job, {"tests/test_app.py": "# edited\n", "tests/test_other.py": "y = 2\n"})
    assert result.modified == ("tests/test_app.py",)
    assert result.created == ("tests/test_other.py",)
    assert (job.clone / "tests/test_app.py").read_text() == "# edited\n"


def test_shared_fixture_in_test_dir_may_be_edited(job):
    # R6 B: the tester may change conftest.py inside a test dir; it's listed as modified.
    result = write_files(job, {"tests/conftest.py": "import pytest\n"})
    assert result.modified == ("tests/conftest.py",)


@pytest.mark.parametrize(
    "path",
    [
        "tests/../src/app/__init__.py",
        "tests/../../outside.py",
        "../tests/test_x.py",
        "/etc/passwd",
        "~/test_x.py",
        "tests\\test_x.py",
        "./tests/test_x.py",
        "tests//test_x.py",
        "tests/./test_x.py",
        "tests/",
        "tests",
        "src/app/test_x.py",
        "README.md",
        "testsuite/test_x.py",
        "tests/.git/hooks/pre-commit",
        "tests/.GIT/config",
        "tests/.claude/settings.json",
        "tests/.mcp.json",
        "",
        "tests/a\x00b.py",
    ],
)
def test_bad_paths_are_rejected(job, path):
    err = rejected(job, {path: "x = 1\n"})
    assert len(err.problems) == 1
    assert not (job.tmp / "outside.py").exists()


def test_non_string_path_and_content_are_rejected(job):
    with pytest.raises(w.TesterOutputInvalid) as e:
        write_tester_files(job.ws, job.repo, [w.TesterFile(None, "x"), w.TesterFile("tests/t.py", b"bytes")])
    assert len(e.value.problems) == 2


def test_symlinked_test_dir_is_rejected(job, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (job.clone / "tests2").symlink_to(outside)
    object.__setattr__(job.repo, "test_dirs", ("tests", "tests2"))
    err = rejected(job, {"tests2/test_x.py": "x = 1\n"})
    assert "symlink" in err.problems[0]
    assert list(outside.iterdir()) == []


def test_symlink_component_inside_test_dir_is_rejected(job, tmp_path):
    outside = tmp_path / "vaultish"
    outside.mkdir()
    (job.clone / "tests" / "sub").symlink_to(outside)
    err = rejected(job, {"tests/sub/test_x.py": "x = 1\n"})
    assert "symlink" in err.problems[0]
    assert list(outside.iterdir()) == []


def test_existing_symlink_file_is_rejected(job, tmp_path):
    target = tmp_path / "target.py"
    target.write_text("original\n")
    (job.clone / "tests" / "test_link.py").symlink_to(target)
    rejected(job, {"tests/test_link.py": "overwrite\n"})
    assert target.read_text() == "original\n"


def test_dangling_symlink_is_rejected(job, tmp_path):
    (job.clone / "tests" / "test_dangling.py").symlink_to(tmp_path / "does-not-exist.py")
    rejected(job, {"tests/test_dangling.py": "x\n"})
    assert not (tmp_path / "does-not-exist.py").exists()


def test_hard_linked_existing_file_is_rejected(job, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("original\n")
    os.link(outside, job.clone / "tests" / "test_hard.py")
    rejected(job, {"tests/test_hard.py": "overwrite\n"})
    assert outside.read_text() == "original\n"


def test_directory_target_and_file_parent_are_rejected(job):
    (job.clone / "tests" / "adir").mkdir()
    err = rejected(job, {"tests/adir": "x\n", "tests/test_app.py/inner.py": "x\n"})
    assert len(err.problems) == 2


def test_twenty_files_allowed_twenty_one_rejected(job):
    files = {f"tests/test_{i}.py": "x = 1\n" for i in range(MAX_TESTER_FILES)}
    assert len(write_files(job, files).created) == MAX_TESTER_FILES
    files[f"tests/test_{MAX_TESTER_FILES}.py"] = "x = 1\n"
    err = rejected(job, files)
    assert "at most 20" in err.problems[0]


def test_size_limit_is_200_kb(job):
    write_files(job, {"tests/test_big.py": "x" * MAX_TESTER_FILE_BYTES})
    err = rejected(job, {"tests/test_bigger.py": "x" * (MAX_TESTER_FILE_BYTES + 1)})
    assert "bytes" in err.problems[0]
    assert MAX_TESTER_FILE_BYTES == 200 * 1024


def test_size_counts_utf8_bytes(job):
    rejected(job, {"tests/test_utf8.py": "é" * (MAX_TESTER_FILE_BYTES // 2 + 1)})


def test_duplicate_paths_are_rejected_case_insensitively(job):
    rejected(job, {"tests/test_a.py": "1\n", "tests/TEST_A.py": "2\n"})


def test_no_files_is_invalid(job):
    rejected(job, {})


def test_one_bad_path_means_nothing_is_written(job):
    rejected(job, {"tests/test_good.py": "x\n", "tests/../evil.py": "x\n"})
    assert not (job.clone / "tests/test_good.py").exists()
    assert not (job.clone / "evil.py").exists()


def test_every_problem_is_listed(job):
    err = rejected(job, {"tests/../a.py": "x", "src/b.py": "x", "tests/ok.py": "x" * (MAX_TESTER_FILE_BYTES + 1)})
    assert len(err.problems) == 3


def test_commit_tester_files_is_first_commit_on_base(job):
    written = write_files(job, {"tests/test_new.py": "def test_x():\n    assert False\n", "tests/test_app.py": "# edited\n"})
    sha = commit_tester_files(job.ws, written, "ENG-123: tester tests")
    assert job.ws.tester_commit == sha
    assert set(job.ws.tester_files) == {"tests/test_new.py", "tests/test_app.py"}
    assert run_git(job.clone, "rev-parse", f"{sha}^") == job.ws.base_sha
    changed = run_git(job.clone, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split()
    assert sorted(changed) == ["tests/test_app.py", "tests/test_new.py"]
    assert run_git(job.clone, "status", "--porcelain") == ""


def test_commit_tester_files_refuses_stray_changes(job):
    (job.clone / "README.md").write_text("changed\n")
    written = write_files(job, {"tests/test_new.py": "x\n"})
    with pytest.raises(WorkspaceError, match="besides the tester files"):
        commit_tester_files(job.ws, written, "tester")


def test_commit_tester_files_refuses_no_op(job):
    original = (job.clone / "tests/test_app.py").read_text()
    written = write_files(job, {"tests/test_app.py": original})
    with pytest.raises(w.TesterOutputInvalid):
        commit_tester_files(job.ws, written, "tester")


def test_commit_tester_files_refuses_when_head_is_not_base(job):
    job.add_tester_commit()
    written = write_files(job, {"tests/test_more.py": "x\n"})
    with pytest.raises(WorkspaceError, match="must sit on the base"):
        commit_tester_files(job.ws, written, "tester")
