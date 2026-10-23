"""Fake claude, codex, srt and node for the doctor tests. No model, sandbox or network is ever
touched. Each fake reads the probe script gaffer wrote and answers every check the way a working
sandbox would (job writes allowed where the surface allows them, everything else denied, only the
probe allowlist reachable), unless its behaviour file overrides an answer. A write the fake reports
as ALLOWED really creates the file, so doctor's stray-file check is exercised too."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from gaffer import children, doctor

FAKE = textwrap.dedent(r'''
    import json, os, pathlib, re, shlex, sys
    me = pathlib.Path(sys.argv[0])
    cfg_path = me.with_name(me.name + ".behaviour.json")
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    argv = sys.argv[1:]
    log = me.with_name(me.name + ".calls.jsonl")
    with open(log, "a") as f:
        f.write(json.dumps({"argv": argv, "env": dict(os.environ), "cwd": os.getcwd()}) + "\n")
    kind = cfg.get("kind", me.name)

    def answer(surface, script_path, overrides):
        """Probe lines for every check in the script, as a working sandbox would answer."""
        text = pathlib.Path(script_path).read_text()
        nonce = re.search(r"^n=(\S+)$", text, re.M).group(1).strip("'")
        out = []
        for line in text.splitlines():
            m = re.match(r"^try (\S+) (head -c 1|stat|ls|touch) (.+)$", line)
            n = re.match(r"^r (\S+) \"\$\(curl .* (\S+) 2>/dev/null\)\"$", line)
            if m:
                label, op, target = m.group(1), m.group(2), shlex.split(m.group(3))[0]
                if op == "touch":
                    job_ok = surface != "codex" and pathlib.Path(target).name.startswith(".doctor-")
                    got = "ALLOWED" if job_ok else "DENIED"
                else:
                    got = "ALLOWED" if target.endswith("/job/repo/README.md") else "DENIED"
            elif n:
                label, target = n.group(1), shlex.split(n.group(2))[0]
                got = "200" if surface in ("claude", "srt") and "pypi.org" in target else "000"
            else:
                continue
            for needle, value in overrides.items():
                if needle in target:
                    got = value
            if m and op == "touch" and got == "ALLOWED":
                pathlib.Path(target).touch()
            out.append(f"GAFFER-PROBE {nonce} {label} {got}")
        return "\n".join(out) + "\n"

    def run_overrides(name):
        runs = cfg.get("runs") or []
        extra = runs[calls_of(name) - 1] if len(runs) >= calls_of(name) else {}
        return {**cfg, **extra}

    def calls_of(name):
        return sum(1 for l in log.read_text().splitlines() if name in json.loads(l)["argv"])

    def emit(ev):
        print(json.dumps(ev), flush=True)

    if argv == ["--version"]:
        print(cfg.get("version", "0.0.0"))
        sys.exit(cfg.get("version_exit", 0))

    if kind == "claude" and argv[:3] == ["auth", "status", "--json"]:
        print(json.dumps(cfg.get("auth", {})))
        sys.exit(0)

    if kind == "claude" and "-p" in argv:
        b = run_overrides("-p")
        prompt = argv[argv.index("-p") + 1]
        sid = argv[argv.index("--session-id") + 1]
        script = re.search(r"bash (\S+doctor-claude\.sh)", prompt).group(1)
        reads = re.findall(r"^- (.+)$", prompt, re.M)
        write = re.search(r"create (\S+) with the content", prompt).group(1)
        init = {"type": "system", "subtype": "init", "apiKeySource": "none", "plugins": [], "mcp_servers": [],
                "tools": ["Bash", "Edit", "Read", "Skill", "StructuredOutput", "Write"], "skills": ["code-loop"],
                "model": "claude-haiku-5-5", "permissionMode": "acceptEdits", "session_id": sid,
                **b.get("init", {})}
        emit(init)
        uses, results = [], []
        if not b.get("skip_reads"):
            for i, p in enumerate(reads):
                uses.append({"type": "tool_use", "id": f"r{i}", "name": "Read", "input": {"file_path": p}})
                res = b.get("read_results", {})
                hit = next((v for k, v in res.items() if k in p), None)
                is_error, text = hit if hit else (True, "<tool_use_error>File is in a directory that is denied by your permission settings.</tool_use_error>")
                results.append({"type": "tool_result", "tool_use_id": f"r{i}", "is_error": is_error, "content": text})
        if not b.get("skip_bash"):
            uses.append({"type": "tool_use", "id": "b0", "name": "Bash", "input": {"command": f"bash {script}"}})
            results.append({"type": "tool_result", "tool_use_id": "b0", "is_error": False,
                            "content": [{"type": "text", "text": answer("claude", script, b.get("outcomes", {}))}]})
        if not b.get("skip_write"):
            uses.append({"type": "tool_use", "id": "w0", "name": "Write", "input": {"file_path": write, "content": "x"}})
            is_error, text = b.get("write_result", [True, "Claude requested permissions to write, but you haven't granted it yet."])
            if not is_error:
                pathlib.Path(write).write_text("x")
            results.append({"type": "tool_result", "tool_use_id": "w0", "is_error": is_error, "content": text})
        emit({"type": "assistant", "message": {"content": uses}})
        emit({"type": "user", "message": {"content": results}})
        projects = ("~/.claude" if b.get("transcript_in_dot_claude") else os.environ["CLAUDE_CONFIG_DIR"])
        projects = pathlib.Path(projects.replace("~", os.environ["HOME"], 1)) / "projects" / "-job-repo"
        if not b.get("no_transcript"):
            projects.mkdir(parents=True, exist_ok=True)
            (projects / f"{sid}.jsonl").write_text("{}\n")
        emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "num_turns": 3,
              "structured_output": {"status": "done", "summary": "probe finished", "question": None}})
        sys.exit(0)

    if kind == "codex" and argv[:2] == ["login", "status"]:
        sys.stderr.write(cfg.get("login", "Logged in using ChatGPT") + "\n")
        sys.exit(0)

    if kind == "codex" and argv[:1] == ["exec"]:
        b = run_overrides("exec")
        cwd = pathlib.Path(argv[argv.index("-C") + 1])
        tid = b.get("thread_id", "01a1fake-0000-7000-8000-00000000000" + str(calls_of("exec")))
        emit({"type": "thread.started", "thread_id": tid})
        if not b.get("skip_script"):
            emit({"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                  "command": "/bin/zsh -lc 'bash doctor-codex.sh'", "exit_code": 0,
                  "aggregated_output": answer("codex", cwd / "doctor-codex.sh", b.get("exec_outcomes", {}))}})
        for ev in b.get("extra_items", []):
            emit({"type": "item.completed", "item": ev})
        result = b.get("result", {"ran_script": True, "web_search_tool_available": False, "web_search_used": False})
        if result is not None:
            pathlib.Path(argv[argv.index("-o") + 1]).write_text(json.dumps(result))
        home = pathlib.Path(os.environ["CODEX_HOME"]) if not b.get("rollout_in_dot_codex") else pathlib.Path(os.environ["CODEX_HOME"]).parents[1] / ".codex"
        state = {"agents_md": {}, "plugins_instructions": False, "apps_instructions": False,
                 "skills": {"includeInstructions": False}, **b.get("world_state", {})}
        if not b.get("no_rollout"):
            d = home / "sessions" / "2026" / "10" / "23"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"rollout-2026-10-23T00-00-00-{tid}.jsonl").write_text(
                json.dumps({"type": "session_meta", "payload": {}}) + "\n"
                + json.dumps({"type": "world_state", "payload": {"state": state}}) + "\n")
        emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
        sys.exit(b.get("exec_exit", 0))

    if kind == "codex" and argv[:1] == ["sandbox"]:
        command = argv[argv.index("--") + 3]
        script = shlex.split(command)[1]
        sys.stdout.write(answer("test", script, cfg.get("sandbox_outcomes", {})))
        sys.exit(0)

    if kind == "srt":
        command = argv[argv.index("-c") + 1]
        script = shlex.split(re.search(r"&& (bash .+)$", command).group(1))[1]
        sys.stdout.write(answer("srt", script, cfg.get("outcomes", {})))
        sys.exit(0)

    if kind == "node":
        sys.exit(0)
    sys.stderr.write(f"fake {kind}: unexpected argv {argv}\n")
    sys.exit(2)
''')

AUTH = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro",
        "email": "mark@example.com", "orgId": "org-1234", "configDirectory": None}


class DoctorRig:
    """Fake CLIs and a fake home for run_doctor."""

    def __init__(self, home: Path, bindir: Path, paths: children.Paths):
        self.home = home
        self.bindir = bindir
        self.paths = paths
        bindir.mkdir(parents=True, exist_ok=True)
        # Deny entries and credential files that exist in a real home (contents never read).
        (home / ".ssh").mkdir()
        (home / ".ssh" / "config").write_text("Host x\n")
        (home / ".claude.json").write_text("{}\n")
        (home / ".claude").mkdir()
        self.install("claude", {"version": "2.1.308 (Claude Code)",
                                "auth": {**AUTH, "configDirectory": str(paths.claude_config_dir)}})
        self.install("codex", {"version": "codex-cli 0.160.0"})
        self.install("node", {})
        self.install(paths.srt, {"kind": "srt"})
        paths.srt_package_json.parent.mkdir(parents=True, exist_ok=True)
        paths.srt_package_json.write_text(json.dumps({"version": children.SRT_VERSION}))

    def _path(self, name: str | Path) -> Path:
        return Path(name) if isinstance(name, Path) else self.bindir / name

    def install(self, name: str | Path, behaviour: dict) -> Path:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!{sys.executable} -S\n{FAKE}")
        path.chmod(0o755)
        self.behave(name, behaviour, replace=True)
        return path

    def behave(self, name: str | Path, behaviour: dict, *, replace: bool = False) -> None:
        p = self._path(name)
        bfile = p.with_name(p.name + ".behaviour.json")
        old = {} if replace or not bfile.exists() else json.loads(bfile.read_text())
        bfile.write_text(json.dumps({**old, **behaviour}))

    def calls(self, name: str | Path) -> list[dict]:
        p = self._path(name)
        log = p.with_name(p.name + ".calls.jsonl")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def model_calls(self) -> dict[str, int]:
        return {"claude": sum(1 for c in self.calls("claude") if "-p" in c["argv"]),
                "codex": sum(1 for c in self.calls("codex") if c["argv"][:1] == ["exec"])}

    def run(self, cfg=None, *, environ: dict | None = None, **kw) -> doctor.DoctorReport:
        env = {"HOME": str(self.home), "USER": "mark", "LANG": "en_GB.UTF-8", "TERM": "xterm-256color", "TMPDIR": "/tmp"}
        return doctor.run_doctor(cfg, paths=self.paths, environ=env if environ is None else environ, home=self.home,
                                 nonce=kw.pop("nonce", "0123456789abcdef"), **kw)


def by_name(report: doctor.DoctorReport) -> dict[str, doctor.ProbeResult]:
    return {p.name: p for p in report.probes}


def failed(report: doctor.DoctorReport) -> list[str]:
    return [p.name for p in report.failed]
