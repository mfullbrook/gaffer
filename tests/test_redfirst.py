"""Red-first classification by exception type (R5 as amended by R28, R22, R29).

The pytest and Jest cases replay Experiment 3 proof D's verdict table; the
PHPUnit cases replay Experiment 3b step 6.
"""

import json

import pytest

from gaffer.reviews.junit import CriterionMap, ExpectedTest, Label, PhpunitFileRun, parse_junit, parse_phpunit_runs
from gaffer.reviews.redfirst import RedFirstStatus, Verdict, classify_red_first, names_symbols
from reviews_fakes import JUNIT

ROOT = "/private/var/folders/gaffer/job/verify"


def cmap_for(runner: str, **override) -> CriterionMap:
    data = json.loads((JUNIT / runner / "tester.json").read_text())
    data.update(override)
    return CriterionMap.from_tester_output(data)


def run_of(runner: str, name: str):
    return parse_junit(runner, (JUNIT / runner / name).read_bytes(), root=ROOT)


def verdicts(result) -> dict[str, Verdict]:
    return {v.test.name: v.verdict for v in result.verdicts}


# --- spike proof D: pytest ------------------------------------------------------------


def without_guard_only_criterion(runner: str) -> CriterionMap:
    """The spike's tester covered C3 with a guard only; see test_guard_only_criterion_fails_coverage."""
    data = json.loads((JUNIT / runner / "tester.json").read_text())
    data["criteria"] = [c for c in data["criteria"] if c["id"] != "C3"]
    return CriterionMap.from_tester_output(data)


def test_pytest_clean_base_is_valid_red():
    r = classify_red_first(without_guard_only_criterion("pytest"), run_of("pytest", "base_clean.xml"))
    assert r.status is RedFirstStatus.OK
    assert verdicts(r) == {
        # collection-level ImportError names word_count (R22)
        "test_word_count_counts_whitespace_separated_words": Verdict.VALID_COLLECTION_ERROR,
        "test_word_count_returns_zero_for_empty_or_whitespace_only_text": Verdict.VALID_COLLECTION_ERROR,
        "test_shout_strips_surrounding_whitespace": Verdict.VALID_ASSERTION,
        "test_shout_uppercases_inner_text": Verdict.GUARD_PASSES,
    }


def test_pytest_typo_is_invalid_red_even_though_junit_says_failure():
    r = classify_red_first(cmap_for("pytest"), run_of("pytest", "base_typo.xml"))
    assert r.status is RedFirstStatus.INVALID
    assert verdicts(r)["test_shout_strips_surrounding_whitespace"] is Verdict.INVALID
    assert "NameError not naming an expected_missing symbol" in r.retry_feedback()
    assert "shuot" in r.retry_feedback()


# --- spike proof D: Jest ----------------------------------------------------------------


def test_jest_clean_base_is_valid_red():
    r = classify_red_first(without_guard_only_criterion("jest"), run_of("jest", "base_clean.xml"))
    assert r.status is RedFirstStatus.OK
    assert verdicts(r) == {
        "wordCount counts words separated by mixed whitespace": Verdict.VALID_MISSING_SYMBOL,
        "wordCount returns zero for empty and whitespace-only text": Verdict.VALID_MISSING_SYMBOL,
        "shout trims surrounding whitespace before upper-casing": Verdict.VALID_ASSERTION,
        "shout upper-cases inner text while preserving inner whitespace": Verdict.GUARD_PASSES,
    }


def test_jest_typo_is_invalid_red():
    r = classify_red_first(cmap_for("jest"), run_of("jest", "base_typo.xml"))
    assert r.status is RedFirstStatus.INVALID
    assert verdicts(r)["shout trims surrounding whitespace before upper-casing"] is Verdict.INVALID
    assert "ReferenceError" in r.retry_feedback()


def test_jest_suite_failure_not_naming_a_symbol_is_invalid():
    r = classify_red_first(cmap_for("jest"), run_of("jest", "base_suite.xml"))
    assert r.status is RedFirstStatus.INVALID
    v = verdicts(r)
    assert v["shout trims surrounding whitespace before upper-casing"] is Verdict.INVALID
    assert v["shout upper-cases inner text while preserving inner whitespace"] is Verdict.INVALID  # a guard must run on base
    assert "Cannot find module" in r.retry_feedback()


# --- Vitest (unverified fixture) ------------------------------------------------------------


def test_vitest_file_load_failure_naming_an_expected_symbol_is_valid_red():
    cmap = CriterionMap(
        (
            ExpectedTest("C1", Label.ACCEPTANCE, "src/__tests__/slug.test.ts", "src/__tests__/slug.test.ts", "slugify > lowercases"),
            ExpectedTest("C2", Label.ACCEPTANCE, "src/__tests__/wordCount.test.ts", "src/__tests__/wordCount.test.ts", "wordCount > counts words"),
            ExpectedTest("C3", Label.ACCEPTANCE, "src/__tests__/shout.test.ts", "src/__tests__/shout.test.ts", "shout > trims surrounding whitespace before upper-casing"),
        ),
        expected_missing=("slugify", "wordCount"),
    )
    r = classify_red_first(cmap, run_of("vitest", "base.xml"))
    assert list(verdicts(r).values()) == [Verdict.VALID_COLLECTION_ERROR, Verdict.VALID_MISSING_SYMBOL, Verdict.VALID_ASSERTION]
    assert r.status is RedFirstStatus.OK


# --- Experiment 3b: PHPUnit (R29) ------------------------------------------------------------


def php_run():
    return parse_phpunit_runs(
        [
            PhpunitFileRun("tests/ShapesTest.php", (JUNIT / "phpunit/ShapesTest.xml").read_bytes()),
            PhpunitFileRun("tests/MissingBaseTest.php", None, (JUNIT / "phpunit/missingbase.out").read_text()),
            PhpunitFileRun("tests/ParseErrorTest.php", None, (JUNIT / "phpunit/parse.out").read_text()),
        ],
        root=ROOT,
    )


def php_test(name: str, file: str = "tests/ShapesTest.php", label: Label = Label.ACCEPTANCE) -> ExpectedTest:
    return ExpectedTest("C1", label, file, "Toy.Tests.ShapesTest" if "Shapes" in file else "Toy.Tests.X", name)


@pytest.mark.parametrize(
    "name, expected_missing, verdict",
    [
        ("testAssertionFailure", (), Verdict.VALID_ASSERTION),
        ("testMissingClass", ("Toy\\WordCounter",), Verdict.VALID_MISSING_SYMBOL),
        ("testMissingFunction", ("Toy\\word_count",), Verdict.VALID_MISSING_SYMBOL),
        ("testTypoUndefinedMethod", ("Toy\\word_count",), Verdict.INVALID),
        ("testRuntimeException", ("Toy\\WordCounter",), Verdict.INVALID),
        ("testTypeError", ("Toy\\WordCounter",), Verdict.INVALID),
        ("testSkipped", (), Verdict.INVALID),
        ("testIncomplete", (), Verdict.INVALID),
        ("testPasses", (), Verdict.ALREADY_PASSING),
    ],
)
def test_phpunit_classification(name, expected_missing, verdict):
    r = classify_red_first(CriterionMap((php_test(name),), expected_missing), php_run())
    assert r.verdicts[0].verdict is verdict


def test_phpunit_fatal_naming_an_expected_symbol_makes_that_files_tests_valid_red():
    t = php_test("testCounts", "tests/MissingBaseTest.php")
    ok = classify_red_first(CriterionMap((t,), ("Toy\\Testing\\WordCase",)), php_run())
    assert (ok.status, ok.verdicts[0].verdict) == (RedFirstStatus.OK, Verdict.VALID_COLLECTION_ERROR)
    bad = classify_red_first(CriterionMap((t,), ("Toy\\WordCounter",)), php_run())
    assert bad.verdicts[0].verdict is Verdict.INVALID
    parse = classify_red_first(CriterionMap((php_test("testX", "tests/ParseErrorTest.php"),), ("Toy\\WordCounter",)), php_run())
    assert parse.status is RedFirstStatus.INVALID and "syntax error" in parse.retry_feedback()


# --- gate rules (R5) ---------------------------------------------------------------------------


def test_acceptance_passing_on_base_is_already_passing_listing_tests_and_criteria():
    r = classify_red_first(cmap_for("pytest"), run_of("pytest", "head_pass.xml"))
    assert r.status is RedFirstStatus.ALREADY_PASSING
    assert {(v.test.name, v.test.criterion_id) for v in r.already_passing} == {
        ("test_word_count_counts_whitespace_separated_words", "C1"),
        ("test_word_count_returns_zero_for_empty_or_whitespace_only_text", "C1"),
        ("test_shout_strips_surrounding_whitespace", "C2"),
    }
    assert verdicts(r)["test_shout_uppercases_inner_text"] is Verdict.GUARD_PASSES


def test_already_passing_wins_over_invalid():
    xml = (
        b'<testsuites><testsuite><testcase classname="t" name="a"/>'
        b'<testcase classname="t" name="b"><failure>E   NameError: name \'x\' is not defined\n\nt.py:3: NameError</failure></testcase>'
        b"</testsuite></testsuites>"
    )
    cmap = CriterionMap((ExpectedTest("C1", Label.ACCEPTANCE, "t.py", "t", "a"), ExpectedTest("C2", Label.ACCEPTANCE, "t.py", "t", "b")))
    assert classify_red_first(cmap, parse_junit("pytest", xml)).status is RedFirstStatus.ALREADY_PASSING


def test_guard_may_fail_on_base_by_assertion_but_not_by_a_typo():
    xml = (
        b'<testsuites><testsuite><testcase classname="t" name="g1"><failure>E   assert 1 == 2\n\nt.py:3: AssertionError</failure></testcase>'
        b'<testcase classname="t" name="g2"><failure>E   NameError: name \'shuot\' is not defined\n\nt.py:5: NameError</failure></testcase>'
        b'<testcase classname="t" name="a"><failure>E   assert 0 == 1\n\nt.py:7: AssertionError</failure></testcase>'
        b"</testsuite></testsuites>"
    )
    cmap = CriterionMap(
        (
            ExpectedTest("C1", Label.GUARD, "t.py", "t", "g1"),
            ExpectedTest("C1", Label.GUARD, "t.py", "t", "g2"),
            ExpectedTest("C1", Label.ACCEPTANCE, "t.py", "t", "a"),
        )
    )
    r = classify_red_first(cmap, parse_junit("pytest", xml))
    assert [v.verdict for v in r.verdicts] == [Verdict.GUARD_FAILS, Verdict.INVALID, Verdict.VALID_ASSERTION]
    assert r.status is RedFirstStatus.INVALID


def test_identity_missing_from_junit_without_a_file_level_error_is_invalid():
    cmap = cmap_for("pytest")
    renamed = CriterionMap(
        tuple(ExpectedTest(t.criterion_id, t.label, t.file, t.classname, t.name + "_typo") if t.name.startswith("test_shout_strips") else t for t in cmap.tests),
        cmap.expected_missing,
        cmap.testable_criteria,
    )
    r = classify_red_first(renamed, run_of("pytest", "base_clean.xml"))
    assert r.status is RedFirstStatus.INVALID
    assert "no JUnit testcase" in r.retry_feedback()


def test_every_testable_criterion_needs_an_acceptance_test():
    data = json.loads((JUNIT / "pytest" / "tester.json").read_text())
    data["criteria"] = [c for c in data["criteria"] if c["id"] != "C3"] + [{"id": "C4", "text": "shout handles None"}]
    r = classify_red_first(CriterionMap.from_tester_output(data), run_of("pytest", "base_clean.xml"))
    assert r.status is RedFirstStatus.INVALID
    assert r.problems == ("criterion C4 has no acceptance test (R5: at least one per testable criterion)",)
    data["untestable"] = ["C4"]
    assert classify_red_first(CriterionMap.from_tester_output(data), run_of("pytest", "base_clean.xml")).status is RedFirstStatus.OK


@pytest.mark.parametrize("runner", ["pytest", "jest"])
def test_guard_only_criterion_fails_coverage(runner):
    """The spike's real tester output covers C3 ("continues to upper-case") with
    a guard only. R5 as written needs an acceptance test per testable criterion,
    so that output is tester_invalid. Flagged in docs/t8-reviews-report.md."""
    r = classify_red_first(cmap_for(runner), run_of(runner, "base_clean.xml"))
    assert r.status is RedFirstStatus.INVALID
    assert r.problems == ("criterion C3 has no acceptance test (R5: at least one per testable criterion)",)
    assert not r.invalid  # every individual test verdict is fine


def test_a_run_that_proves_nothing_is_run_failed():
    r = classify_red_first(cmap_for("jest"), run_of("jest", "empty.xml"))
    assert r.status is RedFirstStatus.RUN_FAILED
    assert all(v.verdict is Verdict.INVALID for v in r.verdicts)


def test_names_symbols_matches_whole_words_by_last_component():
    assert names_symbols("cannot import name 'word_count' from 'toy.strings'", ["toy.strings.word_count"]) == ["toy.strings.word_count"]
    assert names_symbols('Class "Toy\\WordCounter" not found', ["Toy\\WordCounter"]) == ["Toy\\WordCounter"]
    assert names_symbols("name 'word_counter' is not defined", ["toy.strings.word_count"]) == []
    assert names_symbols("wordCountX is not a function", ["wordCount"]) == []
    assert names_symbols("Cannot read properties of undefined (reading 'count')", ["App::count"]) == ["App::count"]
