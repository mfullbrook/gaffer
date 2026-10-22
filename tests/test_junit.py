"""JUnit adapters (R4, R7, R22, R28, R29) and test-inventory matching.

Fixtures in tests/fixtures/junit/: pytest, Jest and PHPUnit are reconstructed
from the spike's evidence; Vitest is written from its documented reporter and
is unverified; generic is synthetic.
"""

import json

import pytest

from gaffer.reviews.junit import (
    GENERIC_XFAIL_NOTE,
    JEST_ASSERTION,
    PHPUNIT_ASSERTION,
    CriterionMap,
    Outcome,
    PhpunitFileRun,
    match_inventory,
    missing_runner_flags,
    parse_junit,
    parse_phpunit_runs,
    phpunit_file_command,
    runner_env,
)
from reviews_fakes import JUNIT

ROOT = "/private/var/folders/gaffer/job/verify"


def junit(path: str) -> bytes:
    return (JUNIT / path).read_bytes()


def cmap_for(runner: str) -> CriterionMap:
    return CriterionMap.from_tester_output(json.loads((JUNIT / runner / "tester.json").read_text()))


def by_name(run, name):
    return next(c for c in run.cases if c.name == name)


# --- never trust exit codes (R4) ------------------------------------------------------


@pytest.mark.parametrize("runner", ["pytest", "jest", "vitest", "phpunit", "generic"])
@pytest.mark.parametrize("content", [None, b"", b"   \n"])
def test_missing_or_empty_junit_is_a_failed_run(runner, content):
    run = parse_junit(runner, content)
    assert not run.usable
    assert "missing or empty" in run.problems[0]


def test_invalid_xml_is_a_failed_run():
    run = parse_junit("pytest", b"<testsuites><testcase")
    assert not run.usable and "not valid XML" in run.problems[0]


def test_failed_run_fails_every_tester_test():
    inv = match_inventory(cmap_for("pytest"), parse_junit("pytest", None))
    assert not inv.all_passed
    assert {e.outcome for e in inv.entries} == {Outcome.MISSING}
    assert all("missing or empty" in e.detail for e in inv.entries)


def test_unknown_runner_is_refused():
    with pytest.raises(ValueError):
        parse_junit("mocha", b"<testsuites/>")


# --- pytest (R28) --------------------------------------------------------------------------


def test_pytest_collection_error_is_a_classname_empty_case_mapped_to_its_file():
    run = parse_junit("pytest", junit("pytest/base_clean.xml"), root=ROOT)
    ce = run.collection_errors
    assert len(ce) == 1
    case = ce[0]
    assert (case.classname, case.name) == ("", "tests.test_word_count")
    assert case.file == "tests/test_word_count.py"
    assert case.exc_type == "ImportError"
    assert "cannot import name 'word_count'" in case.summary
    assert run.collection_error_for("tests/test_word_count.py") is case


def test_pytest_classname_maps_to_file_and_assertions_are_typed():
    run = parse_junit("pytest", junit("pytest/base_clean.xml"))
    case = by_name(run, "test_shout_strips_surrounding_whitespace")
    assert case.file == "tests/test_shout.py"
    assert case.outcome is Outcome.FAILED
    assert (case.exc_type, case.assertion) == ("AssertionError", True)
    typo = by_name(parse_junit("pytest", junit("pytest/base_typo.xml")), "test_shout_strips_surrounding_whitespace")
    assert typo.outcome is Outcome.FAILED  # pytest reports NameError as <failure> ...
    assert (typo.exc_type, typo.assertion) == ("NameError", False)  # ... but it is not an assertion (R28)


def test_pytest_class_based_classname_maps_to_module_file():
    run = parse_junit("pytest", junit("pytest/head_skip_xfail.xml"))
    assert by_name(run, "test_tabs[\\t]").file == "tests/test_word_count.py"
    exists = {"pkg/tests/test_a.py"}.__contains__
    xml = b'<testsuites><testsuite><testcase classname="pkg.tests.test_a.TestX.TestNested" name="test_y"/></testsuite></testsuites>'
    assert parse_junit("pytest", xml, exists=exists).cases[0].file == "pkg/tests/test_a.py"


def test_pytest_skip_and_xfail_are_distinct_and_both_fail_the_inventory():
    run = parse_junit("pytest", junit("pytest/head_skip_xfail.xml"))
    assert by_name(run, "test_shout_strips_surrounding_whitespace").outcome is Outcome.SKIPPED
    assert by_name(run, "test_word_count_counts_whitespace_separated_words").outcome is Outcome.XFAILED
    inv = match_inventory(cmap_for("pytest"), run)
    outcomes = {e.test.name: e.outcome for e in inv.entries}
    assert outcomes == {
        "test_shout_strips_surrounding_whitespace": Outcome.SKIPPED,
        "test_shout_uppercases_inner_text": Outcome.PASSED,
        "test_word_count_counts_whitespace_separated_words": Outcome.XFAILED,
        "test_word_count_returns_zero_for_empty_or_whitespace_only_text": Outcome.MISSING,
    }
    assert not inv.all_passed  # R7: skipped, xfailed and missing all count as failed
    assert len(inv.failing) == 3


def test_pytest_teardown_error_on_a_passed_test_keeps_the_error():
    xml = (
        b'<testsuites><testsuite><testcase classname="tests.test_a" name="test_x"/>'
        b'<testcase classname="tests.test_a" name="test_x"><error message="failed on teardown">E   RuntimeError: x</error></testcase>'
        b"</testsuite></testsuites>"
    )
    run = parse_junit("pytest", xml)
    assert len(run.cases) == 1 and run.cases[0].outcome is Outcome.ERROR


def test_pytest_inventory_on_base_and_on_the_writers_head():
    base = match_inventory(cmap_for("pytest"), parse_junit("pytest", junit("pytest/base_clean.xml")))
    matched = [e for e in base.entries if e.case is not None]
    assert len(matched) == 2  # spike: 2/4 on base, the collection error hides two tests
    assert all(e.collection_error is not None for e in base.entries if e.case is None)
    head = match_inventory(cmap_for("pytest"), parse_junit("pytest", junit("pytest/head_pass.xml")))
    assert head.all_passed  # spike: 4/4 matched and passed on 4db1287


# --- Jest (R28) ------------------------------------------------------------------------------


def test_jest_identities_match_and_failures_are_typed():
    run = parse_junit("jest", junit("jest/base_clean.xml"))
    inv = match_inventory(cmap_for("jest"), run)
    assert all(e.case is not None for e in inv.entries)  # spike: 4/4 matched on base
    trims = by_name(run, "shout trims surrounding whitespace before upper-casing")
    assert (trims.exc_type, trims.assertion, trims.file) == (JEST_ASSERTION, True, "__tests__/shout.test.js")
    wc = by_name(run, "wordCount counts words separated by mixed whitespace")
    assert (wc.exc_type, wc.assertion) == ("TypeError", False)
    assert wc.summary == "TypeError: wordCount is not a function"


def test_jest_reference_error_is_a_failure_but_not_an_assertion():
    case = by_name(parse_junit("jest", junit("jest/base_typo.xml")), "shout trims surrounding whitespace before upper-casing")
    assert case.outcome is Outcome.FAILED
    assert (case.exc_type, case.assertion) == ("ReferenceError", False)


def test_jest_suite_failure_is_one_collection_error_for_its_file():
    run = parse_junit("jest", junit("jest/base_suite.xml"))
    ce = run.collection_errors
    assert len(ce) == 1  # jest-junit's error + failure pair is merged
    assert ce[0].classname == "Test suite failed to run"
    assert ce[0].file == "__tests__/shout.test.js"
    assert "Cannot find module '../src/does-not-exist'" in ce[0].summary
    assert "MODULE_NOT_FOUND" in ce[0].text
    inv = match_inventory(cmap_for("jest"), run)
    shout = [e for e in inv.entries if e.test.file == "__tests__/shout.test.js"]
    assert all(e.outcome is Outcome.MISSING and e.collection_error is ce[0] for e in shout)


def test_jest_empty_junit_is_a_failure_even_though_jest_exited_0():
    run = parse_junit("jest", junit("jest/empty.xml"))
    assert not run.usable
    assert "no testcases" in run.problems[0]


def test_jest_identity_matching_ignores_whitespace_differences():
    run = parse_junit("jest", junit("jest/base_clean.xml"))
    assert run.find("__tests__/strings.test.js", "shout uppercases") is not None


# --- Vitest (unverified fixture) ----------------------------------------------------------------


def test_vitest_shapes():
    run = parse_junit("vitest", junit("vitest/base.xml"), root=ROOT)
    trims = by_name(run, "shout > trims surrounding whitespace before upper-casing")
    assert (trims.file, trims.exc_type, trims.assertion) == ("src/__tests__/shout.test.ts", "AssertionError", True)
    assert by_name(run, "shout > keeps emoji").outcome is Outcome.SKIPPED
    assert (by_name(run, "wordCount > counts words").exc_type, by_name(run, "wordCount > typo").exc_type) == ("TypeError", "ReferenceError")
    ce = run.collection_errors
    assert len(ce) == 1 and ce[0].file == "src/__tests__/slug.test.ts"
    assert "Failed to load url ../slugify" in ce[0].summary
    assert GENERIC_XFAIL_NOTE not in run.notes


# --- PHPUnit (R29) --------------------------------------------------------------------------------


def phpunit_runs():
    return [
        PhpunitFileRun("tests/ShapesTest.php", junit("phpunit/ShapesTest.xml"), "Tests: 9, Assertions: 3, Errors: 5"),
        PhpunitFileRun(f"{ROOT}/tests/MissingBaseTest.php", None, (JUNIT / "phpunit/missingbase.out").read_text()),
        PhpunitFileRun("tests/ParseErrorTest.php", b"", (JUNIT / "phpunit/parse.out").read_text()),
    ]


def test_phpunit_shapes():
    run = parse_phpunit_runs(phpunit_runs(), root=ROOT)
    assert run.usable
    a = by_name(run, "testAssertionFailure")
    assert (a.classname, a.file, a.exc_type, a.assertion) == ("Toy.Tests.ShapesTest", "tests/ShapesTest.php", PHPUNIT_ASSERTION, True)
    for name, typ in [("testRuntimeException", "RuntimeException"), ("testTypeError", "TypeError"), ("testMissingClass", "Error")]:
        c = by_name(run, name)
        assert (c.outcome, c.exc_type, c.assertion) == (Outcome.ERROR, typ, False)
    assert by_name(run, "testMissingClass").summary == 'Error: Class "Toy\\WordCounter" not found'
    assert by_name(run, "testSkipped").outcome is Outcome.SKIPPED
    assert by_name(run, "testIncomplete").outcome is Outcome.SKIPPED  # incomplete is <skipped/> too


def test_phpunit_file_level_fatal_is_a_collection_error_for_that_file_only():
    run = parse_phpunit_runs(phpunit_runs(), root=ROOT)
    fatal = run.collection_error_for("tests/MissingBaseTest.php")
    assert fatal is not None
    assert fatal.summary == 'Class "Toy\\Testing\\WordCase" not found'
    assert "Location: tests/MissingBaseTest.php:5" in fatal.text
    assert run.collection_error_for("tests/ParseErrorTest.php").summary == 'syntax error, unexpected token "}"'
    assert len([c for c in run.cases if not c.collection_error]) == 9  # ShapesTest survives the other files' fatals


def test_phpunit_missing_junit_without_message_lines_still_fails_that_file():
    run = parse_phpunit_runs([PhpunitFileRun("tests/AbcTest.php", None, "Segmentation fault")])
    ce = run.collection_error_for("tests/AbcTest.php")
    assert ce is not None and "no Message: line" in ce.summary
    assert parse_phpunit_runs([]).problems  # nothing ran is a failed run


def test_phpunit_inventory_counts_skipped_and_incomplete_as_failed():
    from gaffer.reviews.junit import ExpectedTest, Label

    cmap = CriterionMap(
        tuple(
            ExpectedTest("C1", Label.ACCEPTANCE, "tests/ShapesTest.php", "Toy.Tests.ShapesTest", n)
            for n in ("testPasses", "testSkipped", "testIncomplete")
        )
    )
    inv = match_inventory(cmap, parse_phpunit_runs(phpunit_runs(), root=ROOT))
    assert [e.outcome for e in inv.entries] == [Outcome.PASSED, Outcome.SKIPPED, Outcome.SKIPPED]
    assert not inv.all_passed


def test_phpunit_per_file_command():
    assert phpunit_file_command("php vendor/bin/phpunit --log-junit junit.xml --do-not-cache-result", "tests/My Test.php") == (
        "php vendor/bin/phpunit --log-junit junit.xml --do-not-cache-result 'tests/My Test.php'"
    )


# --- generic (R22) ----------------------------------------------------------------------------------


def test_generic_states_no_xfail_detection_and_types_untyped_failures():
    run = parse_junit("generic", junit("generic/go.xml"))
    assert GENERIC_XFAIL_NOTE in run.notes
    assert by_name(run, "TestShoutTrims").assertion is True  # untyped, no exception words
    assert by_name(run, "TestWordCount").assertion is False  # a panic is not an assertion
    assert by_name(run, "TestFlaky").outcome is Outcome.SKIPPED


# --- runner settings (R28, R29) ------------------------------------------------------------------------


def test_missing_runner_flags():
    assert missing_runner_flags("pytest", "uv run pytest --junitxml=junit.xml") == ["test_command for runner 'pytest' needs --continue-on-collection-errors"]
    assert missing_runner_flags("pytest", "uv run pytest --continue-on-collection-errors --junitxml=j.xml") == []
    assert missing_runner_flags("jest", "npx jest --ci") == ["test_command for runner 'jest' needs --no-watchman"]
    assert missing_runner_flags("phpunit", "php vendor/bin/phpunit --log-junit=j.xml") == ["test_command for runner 'phpunit' needs --do-not-cache-result"]
    assert missing_runner_flags("generic", "go test ./...") == []


def test_jest_runner_env_matches_the_spike():
    env = runner_env("jest", "/job/junit.xml")
    assert env["JEST_JUNIT_CLASSNAME"] == "{filepath}"
    assert env["JEST_JUNIT_REPORT_TEST_SUITE_ERRORS"] == "true"
    assert env["JEST_JUNIT_OUTPUT_FILE"] == "/job/junit.xml"
    assert runner_env("pytest", "x") == {}
