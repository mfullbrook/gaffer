#!/usr/bin/env python3
"""Proof D: red-first via JUnit (R4, R5, R22), throwaway spike code.

Writes the tester's files into a detached verification worktree at the base
SHA (paths confined to test_dirs, R6), runs the tests under the Codex `test`
permission profile (R20: credentials denied, writes = job dir, no network),
parses JUnit and classifies every criterion-map test.

Usage: python3 -I d_redfirst.py <job-dir> <py|js> <tester.json> <variant> <out.json>
  variant: clean | typo (seed a NameError/ReferenceError into one acceptance
  test) | suite (seed a missing-module import into one test file; js only)
"""
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
HOME = os.environ["HOME"]
TEST_DIRS = {"py": ["tests"], "js": ["__tests__"]}
CLEAN_PATH = f"{HOME}/.local/bin:{HOME}/.bun/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def confined_path(root: Path, rel: str, test_dirs) -> Path:
    """R6: relative, normalised, inside a test dir, no symlink anywhere on the way."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] not in test_dirs:
        raise ValueError(f"tester path rejected: {rel}")
    cur = root
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"tester path crosses a symlink: {rel}")
    if not str(cur.resolve()).startswith(str((root / p.parts[0]).resolve()) + os.sep):
        raise ValueError(f"tester path escapes test dir: {rel}")
    return cur


def codex_test_profile_args(job: str):
    out = sh(["bash", "-c", f'JOB="{job}"; DENY_EXTRA="{HOME}/.gaffer-spike-canary"; '
              f'. "{SPIKE}/codex_profiles.sh"; printf "%s\\0" "${{CODEX_PROFILE_ARGS_test[@]}}"'])
    return [a for a in out.stdout.split("\0") if a] + ["-P", "gaffer_test"]


def run_tests(job: Path, eco: str, verify: Path, junit: Path):
    env = {"HOME": HOME, "PATH": CLEAN_PATH, "USER": os.environ.get("USER", ""), "LANG": "en_GB.UTF-8",
           "TMPDIR": str(job / "tmp"), "CODEX_HOME": f"{HOME}/.gaffer/codex"}
    if eco == "py":
        cmd = [str(job / "repo/.venv/bin/python"), "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "-o", "junit_family=xunit2", "--continue-on-collection-errors", f"--junitxml={junit}"]
    else:
        env.update({"JEST_JUNIT_OUTPUT_FILE": str(junit), "JEST_JUNIT_CLASSNAME": "{filepath}",
                    "JEST_JUNIT_TITLE": "{classname} {title}", "JEST_JUNIT_ADD_FILE_ATTRIBUTE": "true",
                    "JEST_JUNIT_REPORT_TEST_SUITE_ERRORS": "true"})
        cmd = [str(verify / "node_modules/.bin/jest"), "--ci", "--no-watchman", "--reporters=default", "--reporters=jest-junit"]
    full = ["codex", "sandbox", *codex_test_profile_args(str(job)), "-C", str(verify), "--", *cmd]
    r = sh(full, cwd=verify, env=env, stdin=subprocess.DEVNULL)
    return r.returncode, full, (r.stdout + r.stderr)[-1500:]


def parse_junit(path: Path, eco: str):
    cases = []
    for tc in ET.parse(path).getroot().iter("testcase"):
        outcome, msg, text = "passed", "", ""
        for kind in ("failure", "error", "skipped"):
            el = tc.find(kind)
            if el is not None:
                outcome, msg, text = kind, el.get("message", "") or "", el.text or ""
                break
        cls, name = tc.get("classname", ""), tc.get("name", "")
        f = tc.get("file") or ""
        if eco == "py" and not f:
            # xunit2 drops the file attribute: map the module path back to a file;
            # a collection error has classname "" and the module path as its name.
            mod = cls.split(".")
            f = "/".join(mod[:2]) + ".py" if cls else name.replace(".", "/") + ".py"
        cases.append({"file": f, "classname": cls, "name": name, "outcome": outcome,
                      "message": ANSI.sub("", msg)[:300], "text": ANSI.sub("", text)[:800]})
    return cases


ANSI = re.compile(r"\x1b\[[0-9;]*m")
ASSERTION = re.compile(r"^(AssertionError|E +assert|assert )|expect\(received\)|JestAssertionError|Expected|Received", re.M)
EXC = re.compile(r"\b([A-Z][A-Za-z]*(Error|Exception))\b")


def failure_kind(case):
    blob = f"{case['message']}\n{case['text']}"
    exc = EXC.search(blob)
    if case["outcome"] == "failure" and (ASSERTION.search(blob) and (not exc or exc.group(1) == "AssertionError")):
        return "assertion", "AssertionError"
    return "exception", exc.group(1) if exc else "unknown"


def names_missing(case, expected_missing):
    blob = f"{case['message']}\n{case['text']}"
    return [s for s in expected_missing if re.search(rf"\b{re.escape(s.split('.')[-1])}\b", blob)]


def file_level_errors(cases):
    return [c for c in cases if c["outcome"] == "error" and (c["classname"] == "" or
            "suite failed" in (c["classname"] + c["name"] + c["message"] + c["text"]).lower())]


def classify(tester, cases):
    exp = tester["expected_missing"]
    rows = []
    for t in tester["tests"]:
        match = [c for c in cases if c["classname"] == t["classname"] and c["name"] == t["name"]]
        row = {"label": t["label"], "criterion": t["criterion_id"], "identity": f"{t['file']}::{t['name']}"}
        if match:
            c = match[0]
            row["junit"] = c["outcome"]
            if c["outcome"] == "passed":
                row["verdict"] = "guard ok (passes on base)" if t["label"] == "guard" else "BLOCKED already_passing"
            elif c["outcome"] == "skipped":
                row["verdict"] = "INVALID (skipped counts as failed, R7)"
            else:
                kind, exc = failure_kind(c)
                hit = names_missing(c, exp)
                row["cause"] = exc
                if t["label"] == "guard":
                    row["verdict"] = f"guard fails on base ({exc})"
                elif kind == "assertion":
                    row["verdict"] = "VALID red: assertion"
                elif hit:
                    row["verdict"] = f"VALID red: {exc} names expected_missing {hit}"
                else:
                    row["verdict"] = f"INVALID red: {exc} not naming an expected_missing symbol"
        else:
            fl = [c for c in file_level_errors(cases) if c["file"].endswith(t["file"]) or t["file"] in c["name"] + c["classname"]]
            row["junit"] = "no testcase" + (" (file-level error)" if fl else "")
            if fl:
                kind, exc = failure_kind(fl[0])
                hit = names_missing(fl[0], exp)
                row["cause"] = exc
                if hit and t["label"] == "acceptance":
                    row["verdict"] = f"VALID red: collection-level {exc} names expected_missing {hit}"
                else:
                    row["verdict"] = f"INVALID red: collection-level {exc}" + ("" if hit else " not naming expected_missing")
            else:
                row["verdict"] = "INVALID (missing from JUnit counts as failed, R7/R22)"
        rows.append(row)
    return rows


def seed(verify: Path, eco: str, tester, variant):
    acc = next(t for t in tester["tests"] if t["label"] == "acceptance" and ("shout" in t["name"].lower()))
    f = verify / acc["file"]
    src = f.read_text()
    if variant == "typo":
        # Seed a misspelt call inside the body of one acceptance test only.
        key = "def " + acc["name"] if eco == "py" else acc["name"].split(" ", 1)[-1]
        i = src.index(key)
        j = src.index("shout(", i + len(key))
        f.write_text(src[:j] + "shuot(" + src[j + len("shout("):])
        return f"typo shout( -> shuot( in {acc['identity'] if 'identity' in acc else acc['file'] + '::' + acc['name']}"
    if variant == "suite":
        f.write_text("const helper = require('../src/does-not-exist');\n" + src)
        return f"missing module require added to top of {acc['file']}"
    return "none"


def main():
    job, eco, tester_path, variant, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4], Path(sys.argv[5])
    tester = json.loads(tester_path.read_text())
    repo, verify = job / "repo", job / f"verify-{variant}"
    base = sh(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    sh(["git", "-C", str(repo), "worktree", "remove", "--force", str(verify)])
    (job / f"junit-{variant}.xml").unlink(missing_ok=True)
    sh(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(verify), base])
    if eco == "js":
        (verify / "node_modules").symlink_to(repo / "node_modules")
    written = []
    for f in tester["files"]:
        dest = confined_path(verify, f["path"], TEST_DIRS[eco])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f["content"])
        written.append(f["path"])
    seeded = seed(verify, eco, tester, variant) if variant != "clean" else "none"
    junit = job / f"junit-{variant}.xml"
    rc, cmd, tail = run_tests(job, eco, verify, junit)
    cases = parse_junit(junit, eco) if junit.exists() else []
    rows = classify(tester, cases)
    ids = {(t["classname"], t["name"]) for t in tester["tests"]}
    matched = {(c["classname"], c["name"]) for c in cases} & ids
    result = {"eco": eco, "variant": variant, "base": base, "seeded": seeded, "written": written,
              "test_exit": rc, "command": cmd, "junit_cases": cases, "classification": rows,
              "identities": len(ids), "identities_matched_in_junit": len(matched),
              "unmatched": sorted(f"{c}::{n}" for c, n in ids - matched), "runner_tail": tail}
    out.write_text(json.dumps(result, indent=2))
    print(f"[{eco}/{variant}] base={base[:7]} exit={rc} seeded={seeded}")
    print(f"  junit testcases: " + "; ".join(f"{c['classname'] or '<none>'}::{c['name']} {c['outcome']}" for c in cases))
    for r in rows:
        print(f"  {r['label']:<10} {r['identity']:<80} {r['junit']:<28} {r['verdict']}")
    print(f"  identity coverage: {len(matched)}/{len(ids)} matched a JUnit testcase; unmatched={result['unmatched']}")


if __name__ == "__main__":
    main()
