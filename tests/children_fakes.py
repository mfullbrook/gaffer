"""Fake CLI executables and fixtures for the children tests. No real model, GitHub or
network call is ever made: every `claude`, `codex`, `gitleaks` and `srt` here is a small
Python script that records how it was started and replays canned output."""

from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path

import pytest

from gaffer import children
from gaffer.config import load_config

EVIDENCE = Path(__file__).resolve().parents[1] / "spike" / "evidence"

FAKE_CLI = textwrap.dedent(r'''
    import json, os, pathlib, stat, subprocess, sys, time
    me = pathlib.Path(sys.argv[0])
    behaviour_path = me.with_name(me.name + ".behaviour.json")
    cfg = json.loads(behaviour_path.read_text()) if behaviour_path.exists() else {}
    argv = sys.argv[1:]
    try:
        st, null = os.fstat(0), os.stat("/dev/null")
        stdin_devnull = stat.S_ISCHR(st.st_mode) and st.st_rdev == null.st_rdev
    except OSError:
        stdin_devnull = False
    record = {"argv": argv, "env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid(),
              "pgid": os.getpgid(0), "stdin_devnull": stdin_devnull}
    with open(me.with_name(me.name + ".calls.jsonl"), "a") as f:
        f.write(json.dumps(record) + "\n")
    rule = next((r for r in cfg.get("rules", []) if all(t in argv for t in r.get("match", []))), cfg.get("default", {}))
    for flag, content in rule.get("write_after_flag", {}).items():
        pathlib.Path(argv[argv.index(flag) + 1]).write_text(content)
    for path, content in rule.get("write_files", {}).items():
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(path).write_text(content)
    if rule.get("spawn_sleeper"):
        subprocess.Popen(["/bin/sleep", str(rule["spawn_sleeper"])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sys.stderr.write(rule.get("stderr", ""))
    sys.stdout.write(rule.get("stdout", ""))
    sys.stdout.flush()
    for line in rule.get("lines", []):
        print(line, flush=True)
        time.sleep(rule.get("line_delay", 0))
    time.sleep(rule.get("sleep", 0))
    if rule.get("touch"):
        pathlib.Path(rule["touch"]).write_text("acted")
    sys.exit(rule.get("exit", 0))
''')


class Fakes:
    """Installs fake executables and reads back their recorded calls."""

    def __init__(self, bindir: Path):
        self.bindir = bindir
        bindir.mkdir(parents=True, exist_ok=True)

    def install(self, name_or_path: str | Path, behaviour: dict | None = None) -> Path:
        path = Path(name_or_path) if isinstance(name_or_path, Path) else self.bindir / name_or_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!{sys.executable} -S\n{FAKE_CLI}")
        path.chmod(0o755)
        self.behave(path, behaviour or {})
        return path

    def _path(self, name_or_path: str | Path) -> Path:
        return Path(name_or_path) if isinstance(name_or_path, Path) else self.bindir / name_or_path

    def behave(self, name_or_path: str | Path, behaviour: dict) -> None:
        p = self._path(name_or_path)
        p.with_name(p.name + ".behaviour.json").write_text(json.dumps(behaviour))

    def calls(self, name_or_path: str | Path) -> list[dict]:
        p = self._path(name_or_path)
        log = p.with_name(p.name + ".calls.jsonl")
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]


# --- Recorded spike evidence -----------------------------------------------------------


def r2_auth_status() -> dict:
    """The `claude auth status --json` object from spike/evidence/r2_auth.txt."""
    text = (EVIDENCE / "r2_auth.txt").read_text()
    return json.loads(text[text.index("{"): text.index("}") + 1])


def claude_init_extract(run: str) -> dict:
    """The init-event fields recorded for one run (## a1, ## a3, ## a8 ...) in a_claude_init.txt."""
    text = (EVIDENCE / "a_claude_init.txt").read_text()
    m = re.search(rf"^## {re.escape(run)}\n(\{{.*\}})$", text, re.MULTILINE)
    assert m, run
    return json.loads(m.group(1))


def claude_init_event(run: str = "a8", session_id: str | None = None, **overrides) -> dict:
    ev = {"type": "system", "subtype": "init", "cwd": "/job/repo", "model": "claude-opus-5-5",
          "session_id": session_id, **claude_init_extract(run)}
    ev.update(overrides)
    return ev


def claude_result_event(session_id: str, structured: dict | None = None) -> dict:
    """The schema-result event from spike/evidence/a_claude_json_schema.txt, re-keyed to session_id."""
    ev = json.loads((EVIDENCE / "a_claude_json_schema.txt").read_text())
    ev["session_id"] = session_id
    if structured is not None:
        ev["structured_output"] = structured
        ev["result"] = json.dumps(structured)
    ev.setdefault("num_turns", 2)
    ev.setdefault("usage", {"input_tokens": 6, "output_tokens": 40})
    return ev


RATE_LIMIT_ALLOWED = {
    "type": "rate_limit_event",
    "rate_limit_info": {"status": "allowed", "resetsAt": 1792371000, "rateLimitType": "five_hour",
                        "isUsingOverage": False,
                        "unifiedWindows": {"five_hour": {"utilization": 0.06, "resetsAt": 1792371000}}},
}


def spike_tester_output(eco: str = "py", *, r5: bool = True) -> dict:
    """The tester's structured output from spike/evidence/d_tester_<eco>.json, with file contents
    filled in (the evidence keeps only the paths) and an empty untestable list.

    The recorded output has a guard-only criterion (C3: "shout keeps upper-casing"), which R5's
    "at least one acceptance test per testable criterion" rejects (see the T5 report). With
    r5=True the guard test is attached to C2 and C3 is dropped, so the output passes R5 as written."""
    data = json.loads((EVIDENCE / f"d_tester_{eco}.json").read_text())
    data["files"] = [{"path": p, "content": f"# test file {p}\n"} for p in data["files"]]
    data["untestable"] = []
    if r5:
        data["criteria"] = [c for c in data["criteria"] if c["id"] != "C3"]
        for t in data["tests"]:
            if t["criterion_id"] == "C3":
                t["criterion_id"] = "C2"
    return data


def jsonl(*events: dict) -> list[str]:
    return [json.dumps(e) for e in events]


# --- Fixtures --------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "home"
    for d in ("vault", "src/app/.git", ".gaffer/claude", ".gaffer/codex", "gaffer-jobs"):
        (home / d).mkdir(parents=True)
    return home


@pytest.fixture
def fakes(tmp_path) -> Fakes:
    return Fakes(tmp_path / "fakebin")


@pytest.fixture
def paths(home, fakes, tmp_path) -> children.Paths:
    return children.Paths.for_home(
        home, search_path=f"{fakes.bindir}:/usr/bin:/bin", environ={"TMPDIR": "/tmp"},
        claude_managed_dir=tmp_path / "managed" / "ClaudeCode", codex_managed_dir=tmp_path / "managed" / "codex",
    )


def write_config(home: Path, *, ecosystems=("python",), runner="pytest", install_command: str | None = None,
                 allowed_mcp=(), codex_model: str | None = None, writer_model: str | None = None,
                 test_command="uv run pytest -o junit_family=xunit2 --junitxml=junit.xml"):
    lines = ['operator_login = "mfullbrook"', 'vault_path = "~/vault"', f"allowed_mcp = {json.dumps(list(allowed_mcp))}"]
    if codex_model:
        lines.append(f'codex_model = "{codex_model}"')
    if writer_model:
        lines.append(f'writer_model = "{writer_model}"')
    lines += [
        "[repos.ENG]", 'path = "~/src/app"', 'github = "mfullbrook/app"', 'base = "main"',
        f"ecosystems = {json.dumps(list(ecosystems))}", f"test_command = {json.dumps(test_command)}",
        'junit_path = "junit.xml"', f'runner = "{runner}"', 'test_dirs = ["tests"]', 'required_checks = ["test"]',
    ]
    if install_command:
        lines.append(f"install_command = {json.dumps(install_command)}")
    path = home / ".gaffer" / "gaffer.toml"
    path.write_text("\n".join(lines) + "\n")
    cfg = load_config(path, home=home)
    return cfg, cfg.repos["ENG"]


@pytest.fixture
def jobs(home) -> children.JobDirs:
    job = home / "gaffer-jobs" / "ENG-1"
    (job / "repo").mkdir(parents=True)
    return children.JobDirs(job_dir=job, clone=job / "repo", state_dir=home / ".gaffer" / "jobs" / "ENG-1")
