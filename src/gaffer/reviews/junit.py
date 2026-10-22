"""JUnit adapters and test-inventory matching.

Decisions: R4 (JUnit from any runner, never trust exit codes), R7 (skipped,
xfailed or missing tester tests count as failed), R22 (criterion-map identities
must all appear in JUnit; adapters for pytest, Jest, Vitest and generic), R28
(pytest and Jest flags and failure shapes), R29 (PHPUnit per-file runs).

Each adapter turns a runner's JUnit XML into `Case`s with a uniform shape:
identity (classname, name), repo-relative file, outcome, exception type,
whether the failure is an assertion, and an exception summary (the lines
red-first symbol matching reads). File-level failures (pytest collection
errors, Jest "Test suite failed to run", a PHPUnit fatal) become cases with
`collection_error=True` keyed to the file.
"""

from __future__ import annotations

import re
import shlex
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import PurePosixPath

from ._text import as_text, collapse_ws, strip_ansi


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"  # <failure>
    ERROR = "error"  # <error>
    SKIPPED = "skipped"
    XFAILED = "xfailed"  # pytest only (<skipped type="pytest.xfail">)
    MISSING = "missing"  # an expected identity with no testcase (inventory only)


NON_PASS = frozenset(Outcome) - {Outcome.PASSED}

RUNNERS = ("pytest", "jest", "vitest", "phpunit", "generic")

# The exception types that count as an assertion failure (R28, R29).
PHPUNIT_ASSERTION = "PHPUnit\\Framework\\ExpectationFailedException"
JEST_ASSERTION = "JestAssertionError"

# Flags each runner's test_command must carry (R28, R29). gaffer.toml doesn't
# enforce these (T4 is frozen); missing_runner_flags() reports them.
REQUIRED_FLAGS: dict[str, tuple[str, ...]] = {
    "pytest": ("--continue-on-collection-errors",),
    "jest": ("--no-watchman",),
    "vitest": (),
    "phpunit": ("--log-junit", "--do-not-cache-result"),
    "generic": (),
}

# R22: only pytest marks xfail in its JUnit. The generic report must say so.
XFAIL_DETECTION = {"pytest": True, "jest": False, "vitest": False, "phpunit": False, "generic": False}
GENERIC_XFAIL_NOTE = 'xfail detection is unavailable for runner = "generic": an expected-failure test is reported as whatever the runner writes'


@dataclass(frozen=True)
class Case:
    classname: str
    name: str
    file: str  # repo-relative POSIX path, "" if unknown
    outcome: Outcome
    exc_type: str = ""
    assertion: bool = False
    summary: str = ""  # the exception line(s); red-first matches symbols here
    text: str = ""  # full element text and message, ANSI stripped
    collection_error: bool = False

    @property
    def identity(self) -> tuple[str, str]:
        return (self.classname, self.name)

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASSED


@dataclass(frozen=True)
class RunResult:
    """One test run's results. Any problem means the run proves nothing (R4)."""

    runner: str
    cases: tuple[Case, ...]
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return not self.problems

    @property
    def collection_errors(self) -> tuple[Case, ...]:
        return tuple(c for c in self.cases if c.collection_error)

    def find(self, classname: str, name: str) -> Case | None:
        key = (_norm(classname), _norm(name))
        for c in self.cases:
            if not c.collection_error and (_norm(c.classname), _norm(c.name)) == key:
                return c
        return None

    def collection_error_for(self, file: str) -> Case | None:
        want = _norm_path(file)
        for c in self.collection_errors:
            got = _norm_path(c.file)
            if got and want and (got == want or got.endswith("/" + want) or want.endswith("/" + got)):
                return c
        return None


@dataclass(frozen=True)
class PhpunitFileRun:
    """One `phpunit <file>` run (R29): its JUnit bytes (None if no file) and its stdout+stderr."""

    test_file: str
    junit: bytes | str | None
    output: str = ""


# --- entry points -------------------------------------------------------------


def parse_junit(
    runner: str,
    junit: bytes | str | None,
    *,
    root: str | None = None,
    exists: Callable[[str], bool] | None = None,
) -> RunResult:
    """Parse one JUnit file. `root` relativizes absolute file paths; `exists`
    (repo-relative path -> bool) lets pytest map a classname to its file exactly."""
    if runner not in _ADAPTERS:
        raise ValueError(f"unknown runner {runner!r}; expected one of {RUNNERS}")
    notes = (GENERIC_XFAIL_NOTE,) if runner == "generic" else ()
    root_el, problem = _load(junit)
    if problem:
        return RunResult(runner, (), (problem,), notes)
    adapter = _ADAPTERS[runner](root=root, exists=exists)
    cases = _merge_duplicates(adapter.parse(root_el))
    if not cases:
        return RunResult(runner, (), ("JUnit file has no testcases (a run that collected nothing is a failure, R4/R28)",), notes)
    return RunResult(runner, cases, (), notes)


def parse_phpunit_runs(runs: Iterable[PhpunitFileRun], *, root: str | None = None) -> RunResult:
    """Combine per-file PHPUnit runs (R29). A file with no JUnit becomes a
    collection error for that file only, built from its Message:/Location: lines."""
    cases: list[Case] = []
    notes: list[str] = []
    adapter = _Phpunit(root=root, exists=None)
    runs = list(runs)
    if not runs:
        return RunResult("phpunit", (), ("no PHPUnit test files were run",))
    for run in runs:
        test_file = _relpath(run.test_file, root)
        root_el, problem = _load(run.junit)
        file_cases: tuple[Case, ...] = ()
        if not problem:
            file_cases = _merge_duplicates(adapter.parse(root_el))
            if not file_cases:
                problem = "JUnit file has no testcases"
        if problem:
            cases.append(_phpunit_fatal(test_file, run.output, problem, root))
            notes.append(f"{test_file}: {problem}")
        else:
            cases.extend(file_cases)
    return RunResult("phpunit", tuple(cases), (), tuple(notes))


def missing_runner_flags(runner: str, test_command: str) -> list[str]:
    """Flags R28/R29 require on test_command, missing from this one."""
    try:
        words = shlex.split(test_command)
    except ValueError:
        words = test_command.split()
    problems = []
    for flag in REQUIRED_FLAGS.get(runner, ()):
        if not any(w == flag or w.startswith(flag + "=") for w in words):
            problems.append(f"test_command for runner {runner!r} needs {flag}")
    return problems


def runner_env(runner: str, junit_path: str) -> dict[str, str]:
    """Environment for the test run. Jest needs jest-junit configured as in the spike (R28)."""
    if runner == "jest":
        return {
            "JEST_JUNIT_OUTPUT_FILE": junit_path,
            "JEST_JUNIT_CLASSNAME": "{filepath}",
            "JEST_JUNIT_TITLE": "{classname} {title}",
            "JEST_JUNIT_ADD_FILE_ATTRIBUTE": "true",
            "JEST_JUNIT_REPORT_TEST_SUITE_ERRORS": "true",
        }
    return {}


def phpunit_file_command(test_command: str, test_file: str) -> str:
    """R29: PHPUnit runs once per test file so a file-level fatal loses only that file."""
    return f"{test_command} {shlex.quote(test_file)}"


# --- adapters -----------------------------------------------------------------


@dataclass
class _Adapter:
    root: str | None = None
    exists: Callable[[str], bool] | None = None
    runner: str = "generic"

    def parse(self, root_el: ET.Element) -> list[Case]:
        out = []
        for suite, tc in _testcases(root_el):
            out.append(self.case(suite, tc))
        return out

    def case(self, suite: ET.Element | None, tc: ET.Element) -> Case:
        classname = tc.get("classname") or ""
        name = tc.get("name") or ""
        outcome, el = _outcome(tc)
        text = _el_text(el)
        exc_type, summary, assertion = ("", "", False)
        if el is not None and outcome in (Outcome.FAILED, Outcome.ERROR):
            exc_type, summary = self.exception(el, text)
            assertion = self.is_assertion(outcome, exc_type, summary)
        elif outcome in (Outcome.SKIPPED, Outcome.XFAILED):
            summary = collapse_ws(el.get("message") or "") if el is not None else ""
        return Case(
            classname=classname,
            name=name,
            file=self.file(suite, tc),
            outcome=outcome,
            exc_type=exc_type,
            assertion=assertion,
            summary=summary,
            text=text,
            collection_error=False,
        )

    def file(self, suite: ET.Element | None, tc: ET.Element) -> str:
        return _relpath(tc.get("file") or (suite.get("file") if suite is not None else "") or "", self.root)

    def exception(self, el: ET.Element, text: str) -> tuple[str, str]:
        typ = el.get("type") or ""
        summary = _first_lines(text) or collapse_ws(strip_ansi(el.get("message") or ""))
        if not typ:
            typ = _type_prefix(summary)
        return typ, summary

    def is_assertion(self, outcome: Outcome, exc_type: str, summary: str) -> bool:
        if outcome is not Outcome.FAILED:
            return exc_type in _ASSERTION_TYPES
        if exc_type in _ASSERTION_TYPES:
            return True
        # Generic runners: a <failure> with no exception type anywhere is an assertion.
        return not exc_type and not _EXC_WORD.search(summary)


_ASSERTION_TYPES = frozenset({"AssertionError", JEST_ASSERTION, PHPUNIT_ASSERTION})
# Signs of an exception rather than an assertion in an untyped generic failure.
_EXC_WORD = re.compile(r"\b\w*(Error|Exception)\b|\bpanic:|^Traceback|\bnil pointer\b", re.M)


class _Pytest(_Adapter):
    """pytest --junitxml, xunit2 (R28): no file attribute, so classname maps to
    the file; a collection error is classname "" named by module path."""

    _LAST = re.compile(r"^\S.*:\d+: ([A-Za-z_][\w.]*)$")

    def case(self, suite, tc):
        c = super().case(suite, tc)
        if c.classname == "" and c.outcome in (Outcome.ERROR, Outcome.FAILED):
            return replace(c, collection_error=True, file=self._module_file(c.name.split(".")))
        return c

    def file(self, suite, tc):
        if tc.get("file"):  # xunit1
            return _relpath(tc.get("file") or "", self.root)
        parts = [p for p in (tc.get("classname") or "").split(".") if p]
        return self._module_file(parts) if parts else ""

    def _module_file(self, parts: list[str]) -> str:
        if self.exists is not None:
            for k in range(len(parts), 0, -1):
                cand = "/".join(parts[:k]) + ".py"
                if self.exists(cand):
                    return cand
        # Without a filesystem check, drop trailing class names (TestFoo, Nested).
        k = len(parts)
        while k > 1 and parts[k - 1][:1].isupper():
            k -= 1
        return "/".join(parts[:k]) + ".py"

    def exception(self, el, text):
        lines = text.splitlines()
        e_lines = [ln[1:].strip() for ln in lines if ln.startswith("E ")]
        summary = "\n".join(l for l in e_lines if l) or collapse_ws(strip_ansi(el.get("message") or ""))
        typ = ""
        for ln in reversed(lines):
            if ln.strip():
                m = self._LAST.match(ln.strip())
                typ = m.group(1) if m else ""
                break
        if not typ:
            typ = _type_prefix(summary) or _type_prefix(el.get("message") or "")
        return typ, summary

    def is_assertion(self, outcome, exc_type, summary):
        return outcome is Outcome.FAILED and exc_type == "AssertionError"


class _Jest(_Adapter):
    """jest-junit with the spike's env (R28): classname = file path, title =
    "<describe> <test>"; a suite failure is classname "Test suite failed to run"."""

    SUITE_FAILED = "Test suite failed to run"
    _STACK = re.compile(r"^\s+at ")
    _FRAME = re.compile(r"^\s*>?\s*\d+\s*\||^\s*\|")
    _EXPECT = re.compile(r"^(?:Error: )?expect\(")

    def case(self, suite, tc):
        c = super().case(suite, tc)
        if c.classname == self.SUITE_FAILED:
            return replace(c, collection_error=True, file=_relpath(c.name, self.root))
        return c

    def file(self, suite, tc):
        return _relpath(tc.get("file") or tc.get("classname") or "", self.root)

    def exception(self, el, text):
        kept = []
        for ln in text.splitlines():
            if self._STACK.match(ln):
                break
            s = ln.strip()
            if not s or self._FRAME.match(ln) or s.startswith("●"):
                continue
            kept.append(s)
            if len(kept) >= 4:
                break
        summary = "\n".join(kept) or collapse_ws(strip_ansi(el.get("message") or ""))
        first = summary.splitlines()[0] if summary else ""
        if self._EXPECT.match(first):
            return JEST_ASSERTION, summary
        return (el.get("type") or _type_prefix(first) or ("thrown" if first.startswith("thrown:") else "")), summary

    def is_assertion(self, outcome, exc_type, summary):
        return outcome is Outcome.FAILED and exc_type in (JEST_ASSERTION, "AssertionError")


class _Vitest(_Adapter):
    """Vitest's built-in `junit` reporter (jest-junit compatible). UNVERIFIED:
    shapes come from Vitest's documentation and reporter source, not a run.
    classname = file path relative to root, name = "describe > test"; a file
    that fails to load is one testcase named by the file path."""

    def parse(self, root_el):
        out = []
        for suite, tc in _testcases(root_el):
            c = self.case(suite, tc)
            suite_name = suite.get("name") if suite is not None else None
            if (
                suite is not None
                and c.outcome in (Outcome.FAILED, Outcome.ERROR)
                and len(suite.findall("testcase")) == 1
                and suite_name
                and _norm_path(c.name) == _norm_path(suite_name)
            ):
                c = replace(c, collection_error=True, file=_relpath(suite_name, self.root))
            out.append(c)
        return out

    def file(self, suite, tc):
        return _relpath(tc.get("file") or tc.get("classname") or (suite.get("name") if suite is not None else "") or "", self.root)

    def is_assertion(self, outcome, exc_type, summary):
        return outcome is Outcome.FAILED and exc_type == "AssertionError"


class _Phpunit(_Adapter):
    """PHPUnit --log-junit (R29): file attribute is absolute, classname dotted,
    class the real FQCN; assertion type is ExpectationFailedException; skipped
    and incomplete are both <skipped/>."""

    def exception(self, el, text):
        lines = [ln for ln in text.splitlines()]
        # Element text starts with "<Class>::<method>" on its own line.
        if lines and "::" in lines[0] and " " not in lines[0].strip():
            lines = lines[1:]
        kept = []
        for ln in lines:
            if not ln.strip():
                if kept:
                    break
                continue
            kept.append(ln.strip())
        summary = "\n".join(kept)
        return (el.get("type") or _type_prefix(summary)), summary

    def is_assertion(self, outcome, exc_type, summary):
        return outcome is Outcome.FAILED and exc_type == PHPUNIT_ASSERTION


_ADAPTERS: dict[str, type[_Adapter]] = {
    "pytest": _Pytest,
    "jest": _Jest,
    "vitest": _Vitest,
    "phpunit": _Phpunit,
    "generic": _Adapter,
}

_MESSAGE = re.compile(r"^Message:\s+(.*\S)\s*$", re.M)
_LOCATION = re.compile(r"^Location:\s+(.*?):(\d+)\s*$", re.M)


def _phpunit_fatal(test_file: str, output: str, problem: str, root: str | None) -> Case:
    out = strip_ansi(output or "")
    messages = _MESSAGE.findall(out)
    loc = _LOCATION.search(out)
    file = _relpath(loc.group(1), root) if loc else test_file
    if messages:
        summary = "\n".join(messages)
        text = summary + (f"\nLocation: {file}:{loc.group(2)}" if loc else "")
    else:
        summary = f"PHPUnit wrote no usable JUnit for {test_file} ({problem}) and printed no Message: line"
        text = summary
    return Case(
        classname="",
        name=test_file,
        file=file or test_file,
        outcome=Outcome.ERROR,
        exc_type="",
        assertion=False,
        summary=summary,
        text=text,
        collection_error=True,
    )


# --- XML helpers ----------------------------------------------------------------


def _load(junit: bytes | str | None) -> tuple[ET.Element | None, str]:
    text = as_text(junit)
    if not text.strip():
        return None, "JUnit file missing or empty (R4: the exit code is never trusted)"
    try:
        return ET.fromstring(text.encode("utf-8") if isinstance(junit, str) else junit), ""
    except ET.ParseError as e:
        return None, f"JUnit file is not valid XML: {e}"


def _testcases(root_el: ET.Element) -> list[tuple[ET.Element | None, ET.Element]]:
    out: list[tuple[ET.Element | None, ET.Element]] = []

    def walk(el: ET.Element, suite: ET.Element | None) -> None:
        for child in el:
            if child.tag == "testsuite":
                walk(child, child)
            elif child.tag == "testcase":
                out.append((suite, child))
            elif child.tag == "testsuites":
                walk(child, suite)

    if root_el.tag == "testcase":
        return [(None, root_el)]
    walk(root_el, root_el if root_el.tag == "testsuite" else None)
    return out


def _outcome(tc: ET.Element) -> tuple[Outcome, ET.Element | None]:
    for tag, outcome in (("failure", Outcome.FAILED), ("error", Outcome.ERROR)):
        el = tc.find(tag)
        if el is not None:
            return outcome, el
    el = tc.find("skipped")
    if el is not None:
        return (Outcome.XFAILED if el.get("type") == "pytest.xfail" else Outcome.SKIPPED), el
    return Outcome.PASSED, None


def _el_text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    msg = strip_ansi(el.get("message") or "")
    body = strip_ansi(el.text or "")
    if msg and msg.strip() not in body:
        return f"{msg}\n{body}".strip("\n")
    return body.strip("\n")


_TYPE_PREFIX = re.compile(r"^\s*([A-Za-z_$][\w.$\\]*)(?: \[[A-Z_]+\])?:\s")


def _type_prefix(line: str) -> str:
    m = _TYPE_PREFIX.match(line or "")
    if not m:
        return ""
    word = m.group(1)
    # "Error", "TypeError", "Foo\BarException", "pkg.SomeError"; not "Location", "Message".
    last = re.split(r"[.\\]", word)[-1]
    return word if re.search(r"(Error|Exception|Failure)$", last) or last in ("Failed", "Error") else ""


def _first_lines(text: str, limit: int = 4) -> str:
    kept = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            if kept:
                break
            continue
        kept.append(s)
        if len(kept) >= limit:
            break
    return "\n".join(kept)


_RANK = {Outcome.PASSED: 0, Outcome.SKIPPED: 1, Outcome.XFAILED: 1, Outcome.FAILED: 2, Outcome.ERROR: 2}


def _merge_duplicates(cases: Sequence[Case]) -> tuple[Case, ...]:
    """Same identity twice (pytest teardown errors, jest-junit's error+failure
    pair for a suite): keep the worst outcome, joining equal-rank texts."""
    merged: dict[tuple[str, str, bool], Case] = {}
    order: list[tuple[str, str, bool]] = []
    for c in cases:
        key = (c.classname, c.name, c.collection_error)
        prev = merged.get(key)
        if prev is None:
            merged[key] = c
            order.append(key)
        elif _RANK[c.outcome] > _RANK[prev.outcome]:
            merged[key] = c
        elif _RANK[c.outcome] == _RANK[prev.outcome] and c.outcome in NON_PASS:
            merged[key] = replace(
                prev,
                summary="\n".join(x for x in (prev.summary, c.summary) if x),
                text="\n".join(x for x in (prev.text, c.text) if x),
                exc_type=prev.exc_type or c.exc_type,
            )
    return tuple(merged[k] for k in order)


def _relpath(path: str, root: str | None) -> str:
    if not path:
        return ""
    p = path.replace("\\", "/")
    if root:
        r = root.replace("\\", "/").rstrip("/") + "/"
        for prefix in (r, "/private" + r, r.removeprefix("/private")):
            if p.startswith(prefix):
                p = p[len(prefix):]
                break
    return _norm_path(p)


def _norm_path(p: str) -> str:
    if not p:
        return ""
    s = str(PurePosixPath(p.replace("\\", "/")))
    return s.removeprefix("./")


def _norm(s: str) -> str:
    return collapse_ws(s)


# --- inventory (R22) --------------------------------------------------------------


class Label(StrEnum):
    ACCEPTANCE = "acceptance"
    GUARD = "guard"


@dataclass(frozen=True)
class ExpectedTest:
    """One criterion-map entry: the tester's predicted JUnit identity (R22)."""

    criterion_id: str
    label: Label
    file: str
    classname: str
    name: str

    @property
    def identity(self) -> str:
        return f"{self.file}::{self.classname}::{self.name}"


@dataclass(frozen=True)
class CriterionMap:
    """The subset of the tester's output reviews needs (R5, R22). The full
    schema is T5's (children); from_tester_output() reads the spike's shape."""

    tests: tuple[ExpectedTest, ...]
    expected_missing: tuple[str, ...] = ()
    testable_criteria: tuple[str, ...] = ()
    # R5 as amended by R31: criteria the tester marked `preserves` (keep-working
    # behaviour); guard tests alone may cover them.
    preserving_criteria: tuple[str, ...] = ()

    @classmethod
    def from_tester_output(cls, data: dict) -> CriterionMap:
        """Reads children.TESTER_SCHEMA's shape: untestable entries are
        {criterion_id, reason} (a bare id or {id} is also accepted)."""
        tests = tuple(
            ExpectedTest(
                criterion_id=str(t["criterion_id"]),
                label=Label(t["label"]),
                file=_norm_path(str(t["file"])),
                classname=str(t["classname"]),
                name=str(t["name"]),
            )
            for t in data.get("tests", ())
        )
        untestable = {
            str(u.get("criterion_id", u.get("id")) if isinstance(u, dict) else u) for u in data.get("untestable", ())
        }
        criteria = tuple(str(c["id"]) for c in data.get("criteria", ()) if str(c["id"]) not in untestable)
        preserving = tuple(str(c["id"]) for c in data.get("criteria", ()) if c.get("preserves") is True)
        return cls(
            tests=tests,
            expected_missing=tuple(str(s) for s in data.get("expected_missing", ())),
            testable_criteria=criteria,
            preserving_criteria=preserving,
        )


@dataclass(frozen=True)
class InventoryEntry:
    test: ExpectedTest
    outcome: Outcome
    case: Case | None = None
    collection_error: Case | None = None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASSED


@dataclass(frozen=True)
class Inventory:
    entries: tuple[InventoryEntry, ...]
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def all_passed(self) -> bool:
        return not self.problems and all(e.passed for e in self.entries)

    @property
    def failing(self) -> tuple[InventoryEntry, ...]:
        return tuple(e for e in self.entries if not e.passed)


def match_inventory(cmap: CriterionMap, run: RunResult) -> Inventory:
    """Every criterion-map identity must appear in JUnit (R22); any non-pass,
    including skipped, xfailed and missing, counts as failed (R7)."""
    entries = []
    for t in cmap.tests:
        if not run.usable:
            entries.append(InventoryEntry(t, Outcome.MISSING, detail="; ".join(run.problems)))
            continue
        case = run.find(t.classname, t.name)
        if case is not None:
            detail = "" if case.passed else f"{case.outcome.value}: {case.summary or case.exc_type}".strip()
            entries.append(InventoryEntry(t, case.outcome, case=case, detail=detail))
            continue
        ce = run.collection_error_for(t.file)
        if ce is not None:
            entries.append(InventoryEntry(t, Outcome.MISSING, collection_error=ce, detail=f"file-level error: {ce.summary}"))
        else:
            entries.append(InventoryEntry(t, Outcome.MISSING, detail="no JUnit testcase with this classname and name"))
    return Inventory(tuple(entries), run.problems, run.notes)
