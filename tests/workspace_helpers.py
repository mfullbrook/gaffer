"""Shared helpers for the workspace tests (T7): disposable git repos, a fake
gitleaks and a fake GitHub. Nothing here touches the network or real remotes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.parse
from pathlib import Path
from typing import Any

from gaffer import config as gconfig
from gaffer import workspace as ws_mod
from gaffer.config import Secret

TEST_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/"),
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "writer",
    "GIT_AUTHOR_EMAIL": "writer@example.invalid",
    "GIT_COMMITTER_NAME": "writer",
    "GIT_COMMITTER_EMAIL": "writer@example.invalid",
}

# A fake token, built at runtime so no token-shaped literal sits in the repo.
FAKE_TOKEN = "ghp_" + "T0kenValue" * 3 + "abcdef"


def run_git(cwd: Path, *args: str, input: str | None = None, check: bool = True) -> str:
    """Plain git, as the writer (or Mark) would run it: no gaffer safety settings."""
    p = subprocess.run(["git", *args], cwd=cwd, env=TEST_ENV, input=input, capture_output=True, text=True, check=False)
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr}")
    return p.stdout.strip()


def write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


_TEMPLATE: Path | None = None


def _template() -> Path:
    """Build the source repo once per session; each test gets a copy."""
    global _TEMPLATE
    if _TEMPLATE is None:
        import tempfile

        _TEMPLATE = Path(tempfile.mkdtemp(prefix="gaffer-t7-template-")) / "app"
        _build_source(_TEMPLATE)
    return _TEMPLATE


def make_source(tmp: Path) -> Path:
    """Mark's checkout: a small python repo with tests, harness files and CI."""
    import shutil

    src = tmp / "home" / "src" / "app"
    src.parent.mkdir(parents=True)
    shutil.copytree(_template(), src, symlinks=True)
    return src


def _build_source(src: Path) -> Path:
    src.mkdir(parents=True)
    run_git(src, "init", "-q", "-b", "main")
    write(src, "src/app/__init__.py", "def add(a, b):\n    return a + b\n")
    write(src, "tests/test_app.py", "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    write(src, "tests/conftest.py", "")
    write(src, "pyproject.toml", "[project]\nname = 'app'\n")
    write(src, ".github/workflows/ci.yml", "name: ci\n")
    write(src, "README.md", "app\n")
    run_git(src, "add", "-A")
    run_git(src, "commit", "-q", "-m", "initial")
    return src


def make_remote(tmp: Path, src: Path) -> Path:
    """A bare repo standing in for GitHub."""
    remote = tmp / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(remote)], env=TEST_ENV, check=True, capture_output=True)
    return remote


def make_config(tmp: Path, src: Path, *, ecosystems: str = '["python"]', runner: str = "pytest", extra: str = "") -> gconfig.Config:
    home = tmp / "home"
    (home / "vault").mkdir(parents=True, exist_ok=True)
    (home / ".gaffer").mkdir(parents=True, exist_ok=True)
    path = home / ".gaffer" / "gaffer.toml"
    path.write_text(textwrap.dedent(f"""\
        operator_login = "mfullbrook"
        vault_path = "~/vault"
        job_root = "{tmp / 'jobs'}"

        [repos.ENG]
        path = "{src}"
        github = "mfullbrook/app"
        base = "main"
        ecosystems = {ecosystems}
        test_command = "uv run pytest --junitxml=junit.xml"
        junit_path = "junit.xml"
        runner = "{runner}"
        test_dirs = ["tests"]
        required_checks = ["test"]
        """) + extra)
    return gconfig.load_config(path, home=home)


class Job:
    """A workspace with its config, source and fake-GitHub remote."""

    def __init__(self, tmp: Path, *, ecosystems: str = '["python"]', runner: str = "pytest", extra: str = ""):
        self.tmp = tmp
        self.src = make_source(tmp)
        self.remote = make_remote(tmp, self.src)
        self.config = make_config(tmp, self.src, ecosystems=ecosystems, runner=runner, extra=extra)
        self.repo = self.config.repos["ENG"]
        self.ws = ws_mod.create_workspace(
            self.config, self.repo, "ENG-123", "gaffer/ENG-123", remote_url=str(self.remote)
        )

    @property
    def clone(self) -> Path:
        return self.ws.clone

    def add_tester_commit(self, files: dict[str, str] | None = None) -> str:
        files = files or {"tests/test_feature.py": "def test_feature():\n    assert False\n"}
        tf = [ws_mod.TesterFile(p, c) for p, c in files.items()]
        written = ws_mod.write_tester_files(self.ws, self.repo, tf)
        return ws_mod.commit_tester_files(self.ws, written, "ENG-123: tester tests")

    def writer_commit(self, files: dict[str, str | None], message: str = "implement") -> str:
        """Commit as the writer would. A None value deletes the file."""
        for rel, content in files.items():
            if content is None:
                (self.clone / rel).unlink()
            else:
                write(self.clone, rel, content)
        run_git(self.clone, "add", "-A")
        run_git(self.clone, "commit", "-q", "-F", "-", input=message)
        return run_git(self.clone, "rev-parse", "HEAD")

    def head(self) -> str:
        return run_git(self.clone, "rev-parse", "HEAD")


# --- fake gitleaks --------------------------------------------------------------

FAKE_GITLEAKS = r'''#!{python}
import json, os, re, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_GITLEAKS_LOG") or {log!r}
if log:
    with open(log, "a") as f:
        f.write(json.dumps({{"argv": args, "cwd": os.getcwd(), "env": dict(os.environ), "files": os.listdir(".")}}) + "\n")
mode = {mode!r}
if mode == "crash":
    sys.stderr.write("boom\n"); sys.exit(2)
assert args[0] == "stdin", args
def opt(name):
    return args[args.index(name) + 1]
text = sys.stdin.read()
findings = []
for n, line in enumerate(text.split("\n"), start=1):
    for m in re.finditer(r"FAKESECRET_[A-Z0-9]{{12}}", line):
        findings.append({{"RuleID": "fake-rule", "Description": "Fake secret", "StartLine": n,
                          "Secret": m.group(0), "Match": "token=" + m.group(0), "File": "", "Commit": ""}})
if mode == "no-report":
    sys.exit(int(opt("--exit-code")) if findings else 0)
with open(opt("--report-path"), "w") as f:
    json.dump(findings if mode != "garbage" else {{"not": "a list"}}, f)
sys.exit(int(opt("--exit-code")) if findings else 0)
'''


def fake_gitleaks(tmp: Path, *, mode: str = "normal", log: Path | None = None) -> Path:
    tools = tmp / "tools"
    tools.mkdir(exist_ok=True)
    path = tools / f"gitleaks-{mode}"
    path.write_text(FAKE_GITLEAKS.format(python=sys.executable, mode=mode, log=str(log) if log else ""))
    path.chmod(0o755)
    return path


def fake_secret(tag: str = "A") -> str:
    return "FAKESECRET_" + (tag * 12)[:12]


# --- fake GitHub ----------------------------------------------------------------


class FakeGitHub:
    """An in-memory GitHub REST API for one repo. Records every request."""

    def __init__(self, github: str = "mfullbrook/app", *, login: str = "mfullbrook", page_size: int = 100):
        self.github = github
        self.login = login
        self.page_size = page_size
        self.pulls: list[dict[str, Any]] = []
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.requests: list[dict[str, Any]] = []
        self.fail_with: tuple[int, dict[str, Any]] | None = None
        self._next_id = 1000

    def add_pull(self, *, branch: str, body: str, number: int = 7, state: str = "open") -> dict[str, Any]:
        pr = {"number": number, "html_url": f"https://github.com/{self.github}/pull/{number}", "state": state,
              "draft": False, "body": body, "head": {"ref": branch}, "title": "existing"}
        self.pulls.append(pr)
        return pr

    def add_comment(self, number: int, body: str, login: str) -> dict[str, Any]:
        self._next_id += 1
        c = {"id": self._next_id, "body": body, "user": {"login": login},
             "html_url": f"https://github.com/{self.github}/pull/{number}#issuecomment-{self._next_id}"}
        self.comments.setdefault(number, []).append(c)
        return c

    def _resp(self, status: int, data: Any, headers: dict[str, str] | None = None) -> ws_mod.HttpResponse:
        return ws_mod.HttpResponse(status, headers or {}, json.dumps(data).encode())

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body) if body else None
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "payload": payload})
        if self.fail_with:
            return self._resp(*self.fail_with)
        base = f"https://api.github.com/repos/{self.github}"
        path, _, query = url.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        if path == "https://api.github.com/user":
            return self._resp(200, {"login": self.login})
        if path == f"{base}/pulls" and method == "GET":
            owner = self.github.split("/")[0]
            head = params.get("head", "")
            found = [p for p in self.pulls if f"{owner}:{p['head']['ref']}" == head and p["state"] == params.get("state", "open")]
            return self._resp(200, found)
        if path == f"{base}/pulls" and method == "POST":
            number = 100 + len(self.pulls)
            pr = {"number": number, "html_url": f"https://github.com/{self.github}/pull/{number}", "state": "open",
                  "draft": payload.get("draft", False), "body": payload["body"], "title": payload["title"],
                  "head": {"ref": payload["head"]}, "base": {"ref": payload["base"]}}
            self.pulls.append(pr)
            return self._resp(201, pr)
        m = re.fullmatch(rf"{re.escape(base)}/issues/(\d+)/comments", path)
        if m and method == "GET":
            number = int(m.group(1))
            items = self.comments.get(number, [])
            page = int(params.get("page", "1"))
            chunk = items[(page - 1) * self.page_size: page * self.page_size]
            headers = {}
            if page * self.page_size < len(items):
                headers["Link"] = f'<{path}?per_page=100&page={page + 1}>; rel="next"'
            return self._resp(200, chunk, headers)
        if m and method == "POST":
            return self._resp(201, self.add_comment(int(m.group(1)), payload["body"], self.login))
        return self._resp(404, {"message": "Not Found"})


def client(fake: FakeGitHub) -> ws_mod.GitHubClient:
    return ws_mod.GitHubClient(fake.github, Secret(FAKE_TOKEN), transport=fake)
