"""Red-first classification of the tester's tests on the base (R5 as amended by R28, R22, R29).

On the base an acceptance test is a valid red when:
- it fails by assertion (pytest AssertionError, Jest expect(), Vitest
  AssertionError, PHPUnit ExpectationFailedException), or
- it fails with any other exception whose summary names an expected_missing
  symbol, or
- it has no testcase because its file failed at collection and that
  file-level error names an expected_missing symbol (R22).
Anything else is invalid: one retry with the errors attached, then BLOCKED:
tester_invalid. An acceptance test passing on base is BLOCKED: already_passing.
Guard tests may pass on base.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .junit import Case, CriterionMap, ExpectedTest, Label, Outcome, RunResult


class Verdict(StrEnum):
    VALID_ASSERTION = "valid_assertion"
    VALID_MISSING_SYMBOL = "valid_missing_symbol"
    VALID_COLLECTION_ERROR = "valid_collection_error"
    ALREADY_PASSING = "already_passing"
    GUARD_PASSES = "guard_passes"
    GUARD_FAILS = "guard_fails_on_base"
    INVALID = "invalid"


VALID_RED = frozenset({Verdict.VALID_ASSERTION, Verdict.VALID_MISSING_SYMBOL, Verdict.VALID_COLLECTION_ERROR})


class RedFirstStatus(StrEnum):
    OK = "ok"  # commit the tester files
    INVALID = "tester_invalid"  # one retry with retry_feedback(), then BLOCKED
    ALREADY_PASSING = "already_passing"  # BLOCKED, listing tests and criteria
    RUN_FAILED = "run_failed"  # the run itself proved nothing (no JUnit, nothing collected)


@dataclass(frozen=True)
class TestVerdict:
    __test__ = False  # not a pytest class

    test: ExpectedTest
    verdict: Verdict
    reason: str
    case: Case | None = None


@dataclass(frozen=True)
class RedFirstResult:
    status: RedFirstStatus
    verdicts: tuple[TestVerdict, ...]
    problems: tuple[str, ...] = ()  # coverage gaps and run-level problems

    @property
    def invalid(self) -> tuple[TestVerdict, ...]:
        return tuple(v for v in self.verdicts if v.verdict is Verdict.INVALID)

    @property
    def already_passing(self) -> tuple[TestVerdict, ...]:
        return tuple(v for v in self.verdicts if v.verdict is Verdict.ALREADY_PASSING)

    def retry_feedback(self) -> str:
        """The errors attached to the tester's one retry (R5)."""
        lines = [f"- {p}" for p in self.problems]
        for v in self.invalid:
            lines.append(f"- {v.test.identity} ({v.test.label.value}, {v.test.criterion_id}): {v.reason}")
        return "\n".join(lines)


def names_symbols(text: str, symbols: Iterable[str]) -> list[str]:
    """expected_missing symbols named in text, by full name or last component
    (`toy.strings.word_count` -> `word_count`, `Toy\\WordCounter` -> `WordCounter`)."""
    hits = []
    for sym in symbols:
        parts = [p for p in re.split(r"::|[.\\/#]", sym) if p]
        if not parts:
            continue
        if any(re.search(rf"(?<![\w$]){re.escape(s)}(?![\w$])", text) for s in (sym, parts[-1])):
            hits.append(sym)
    return hits


def classify_red_first(cmap: CriterionMap, base: RunResult) -> RedFirstResult:
    if not base.usable:
        verdicts = tuple(TestVerdict(t, Verdict.INVALID, "; ".join(base.problems)) for t in cmap.tests)
        return RedFirstResult(RedFirstStatus.RUN_FAILED, verdicts, base.problems)

    verdicts = tuple(_classify(t, base, cmap.expected_missing) for t in cmap.tests)
    problems = []
    covered = {t.criterion_id for t in cmap.tests if t.label is Label.ACCEPTANCE}
    for crit in cmap.testable_criteria:
        if crit not in covered:
            problems.append(f"criterion {crit} has no acceptance test (R5: at least one per testable criterion)")
    if not cmap.tests:
        problems.append("the criterion map lists no tests")

    if any(v.verdict is Verdict.ALREADY_PASSING for v in verdicts):
        status = RedFirstStatus.ALREADY_PASSING
    elif problems or any(v.verdict is Verdict.INVALID for v in verdicts):
        status = RedFirstStatus.INVALID
    else:
        status = RedFirstStatus.OK
    return RedFirstResult(status, verdicts, tuple(problems))


def _classify(t: ExpectedTest, run: RunResult, expected_missing: tuple[str, ...]) -> TestVerdict:
    guard = t.label is Label.GUARD
    case = run.find(t.classname, t.name)
    if case is None:
        ce = run.collection_error_for(t.file)
        if ce is None:
            return TestVerdict(t, Verdict.INVALID, "no JUnit testcase with this classname and name, and no file-level error for its file (R22)")
        hit = names_symbols(ce.summary, expected_missing)
        if hit:
            if guard:
                return TestVerdict(t, Verdict.GUARD_FAILS, f"file-level error names expected_missing {hit}: {ce.summary}", ce)
            return TestVerdict(t, Verdict.VALID_COLLECTION_ERROR, f"file-level error names expected_missing {hit}", ce)
        return TestVerdict(t, Verdict.INVALID, f"file-level error not naming an expected_missing symbol: {ce.summary or ce.exc_type}", ce)

    if case.outcome is Outcome.PASSED:
        if guard:
            return TestVerdict(t, Verdict.GUARD_PASSES, "guard passes on base", case)
        return TestVerdict(t, Verdict.ALREADY_PASSING, f"acceptance test passes on base (criterion {t.criterion_id})", case)
    if case.outcome in (Outcome.SKIPPED, Outcome.XFAILED):
        return TestVerdict(t, Verdict.INVALID, f"{case.outcome.value} on base; a tester test must run (R7)", case)

    if case.assertion:
        if guard:
            return TestVerdict(t, Verdict.GUARD_FAILS, f"guard fails by assertion on base: {case.summary}", case)
        return TestVerdict(t, Verdict.VALID_ASSERTION, f"assertion failure ({case.exc_type})", case)
    hit = names_symbols(case.summary, expected_missing)
    if hit:
        if guard:
            return TestVerdict(t, Verdict.GUARD_FAILS, f"{case.exc_type or 'error'} names expected_missing {hit}", case)
        return TestVerdict(t, Verdict.VALID_MISSING_SYMBOL, f"{case.exc_type or 'error'} names expected_missing {hit}", case)
    return TestVerdict(
        t, Verdict.INVALID, f"{case.exc_type or 'error'} not naming an expected_missing symbol: {case.summary}", case
    )
