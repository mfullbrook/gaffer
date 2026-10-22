"""Required-check evaluation (R11)."""

import pytest

from gaffer.config import DEFAULT_CHECK_APPEAR_TIMEOUT
from gaffer.reviews.checks import CheckState, evaluate_required_checks
from reviews_fakes import load

T = DEFAULT_CHECK_APPEAR_TIMEOUT  # 10 min


def run(name, conclusion=None, status="completed", id=1, started_at="2026-10-20T08:15:51Z"):
    return {"id": id, "name": name, "status": status, "conclusion": conclusion, "started_at": started_at}


def evaluate(runs, required=("test",), skippable=(), since=60, statuses=()):
    return evaluate_required_checks(list(required), skippable, runs, statuses=statuses, seconds_since_push=since, appear_timeout=T)


def state(result, name="test"):
    return next(v for v in result.verdicts if v.name == name).state


@pytest.mark.parametrize(
    "conclusion, skippable, expected",
    [
        ("success", (), CheckState.PASS),
        ("skipped", ("test",), CheckState.PASS),
        ("neutral", ("test",), CheckState.PASS),
        ("skipped", (), CheckState.FAIL),
        ("neutral", (), CheckState.FAIL),
        ("failure", (), CheckState.FAIL),
        ("cancelled", (), CheckState.FAIL),
        ("timed_out", (), CheckState.FAIL),
        ("action_required", (), CheckState.FAIL),
        ("failure", ("test",), CheckState.FAIL),  # skippable only softens skipped/neutral
        ("stale", (), CheckState.FAIL),  # unknown conclusions fail closed
        ("startup_failure", (), CheckState.FAIL),
    ],
)
def test_conclusion_mapping(conclusion, skippable, expected):
    assert state(evaluate([run("test", conclusion)], skippable=skippable)) is expected


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending"])
def test_unfinished_check_is_pending(status):
    r = evaluate([run("test", None, status=status)], since=T * 3)
    assert state(r) is CheckState.PENDING
    assert not r.concluded


def test_absent_check_is_pending_until_check_appear_timeout_then_missing():
    assert state(evaluate([], since=T - 1)) is CheckState.PENDING
    r = evaluate([run("lint", "success")], since=T)
    assert state(r) is CheckState.MISSING
    assert [v.name for v in r.missing] == ["test"]
    assert "600 s after push" in r.missing[0].reason
    assert not r.concluded and not r.all_passed


def test_concluded_and_all_passed():
    r = evaluate([run("test", "success"), run("lint", "failure", id=2)], required=("test", "lint"))
    assert r.concluded and not r.all_passed
    assert [v.name for v in r.failed] == ["lint"]
    r = evaluate([run("test", "success"), run("lint", "in_progress", status="in_progress", id=2)], required=("test", "lint"))
    assert not r.concluded


def test_a_rerun_check_uses_its_latest_run():
    runs = [
        run("test", "failure", id=10, started_at="2026-10-20T08:15:51Z"),
        run("test", "success", id=11, started_at="2026-10-20T08:20:00Z"),
    ]
    assert state(evaluate(runs)) is CheckState.PASS
    assert state(evaluate(list(reversed(runs)))) is CheckState.PASS


def test_commit_status_contexts_count_when_no_check_run_has_the_name():
    statuses = [
        {"id": 1, "context": "ci/circleci", "state": "pending", "created_at": "2026-10-20T08:00:00Z"},
        {"id": 2, "context": "ci/circleci", "state": "failure", "description": "1 failed", "created_at": "2026-10-20T08:05:00Z"},
    ]
    r = evaluate([], required=("ci/circleci",), statuses=statuses)
    assert state(r, "ci/circleci") is CheckState.FAIL


def test_empty_required_checks_means_no_ci_evidence():
    r = evaluate_required_checks([], [], [], seconds_since_push=0, appear_timeout=T)
    assert r.no_ci_evidence and r.concluded and r.all_passed and r.verdicts == ()


def test_ci_failed_check_fixture_fails():
    check_run = load("ci_failed_check/check_run.json")
    r = evaluate([check_run], required=("test",))
    v = r.verdicts[0]
    assert (v.state, v.conclusion) == (CheckState.FAIL, "failure")
    assert v.check_run["id"] == load("ci_failed_check/expected.json")["item"]["check_run_id"]
