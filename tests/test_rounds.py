"""Rounds: completion, settle, late items, supervised-time deadlines (R25), and the
GitHub read client the poller uses (no network: fakes and a fake opener)."""

import io
import json
import urllib.error
from pathlib import Path

import pytest

from gaffer.config import RepoConfig, Secret
from gaffer.reviews.checks import evaluate_required_checks
from gaffer.reviews.coderabbit import CodeRabbitState, CodeRabbitStatus
from gaffer.reviews.github import GitHubError, UrllibGitHubReader
from gaffer.reviews.rounds import (
    SETTLE_SECONDS,
    Round,
    RoundState,
    RoundStatus,
    poll_round,
)
from reviews_fakes import FakeClock, FakeGitHub, load

TIMEOUT = 30 * 60
DONE = CodeRabbitStatus(CodeRabbitState.COMPLETE, "Review completed")
PENDING = CodeRabbitStatus(CodeRabbitState.PENDING, "status pending")
PAUSED = CodeRabbitStatus(CodeRabbitState.UNVERIFIED, "CodeRabbit will not review this head: status 'Review paused'")


def checks(state="success", required=("test",), since=60):
    runs = [{"id": 1, "name": n, "status": "completed" if state else "in_progress", "conclusion": state} for n in required]
    return evaluate_required_checks(list(required), [], runs, seconds_since_push=since, appear_timeout=600)


def new_round(clock=None):
    clock = clock or FakeClock()
    return Round.open("h1", clock=clock, round_timeout=TIMEOUT), clock


def observe(rnd, *, c=None, codex=True, cr=DONE, keys=()):
    return rnd.observe(checks=c or checks(), codex_recorded=codex, coderabbit=cr, pending_keys=keys)


K1 = ("coderabbit", "1", "a")
K2 = ("operator", "2", "b")
K3 = ("coderabbit", "3", "c")


# --- completion ------------------------------------------------------------------------


def test_round_completes_after_all_conditions_and_a_60s_settle():
    rnd, clock = new_round()
    out = observe(rnd, keys={K1})
    assert out.status is RoundStatus.WAITING and out.waiting_on[0].startswith("settle")
    clock.advance(SETTLE_SECONDS - 1)
    assert observe(rnd, keys={K1}).status is RoundStatus.WAITING
    clock.advance(1)
    out = observe(rnd, keys={K1})
    assert out.status is RoundStatus.COMPLETE
    assert out.packet_keys == {K1}
    assert rnd.state.packet_keys == {K1}


@pytest.mark.parametrize(
    "kw, waiting",
    [
        ({"c": checks(state=None)}, "required checks"),
        ({"codex": False}, "Codex review"),
        ({"cr": PENDING}, "CodeRabbit"),
    ],
)
def test_each_condition_is_required(kw, waiting):
    rnd, clock = new_round()
    observe(rnd, **kw)
    clock.advance(10 * SETTLE_SECONDS)
    out = observe(rnd, **kw)
    assert out.status is RoundStatus.WAITING
    assert any(w.startswith(waiting) for w in out.waiting_on)


def test_failed_required_checks_still_conclude_the_round():
    rnd, clock = new_round()
    observe(rnd, c=checks("failure"))
    clock.advance(SETTLE_SECONDS)
    out = observe(rnd, c=checks("failure"))
    assert out.status is RoundStatus.COMPLETE and [v.name for v in out.checks.failed] == ["test"]


def test_settle_starts_when_coderabbit_is_done():
    rnd, clock = new_round()
    observe(rnd, cr=PENDING)
    clock.advance(5 * 60)
    assert observe(rnd, cr=DONE).status is RoundStatus.WAITING  # done just now: settle begins
    clock.advance(SETTLE_SECONDS)
    assert observe(rnd, cr=DONE).status is RoundStatus.COMPLETE


def test_settle_resets_on_each_new_admitted_item():
    rnd, clock = new_round()
    observe(rnd, keys={K1})
    clock.advance(50)
    observe(rnd, keys={K1, K2})  # new item at 50 s
    clock.advance(50)
    assert observe(rnd, keys={K1, K2}).status is RoundStatus.WAITING  # only 50 s since K2
    clock.advance(10)
    out = observe(rnd, keys={K1, K2})
    assert out.status is RoundStatus.COMPLETE and out.packet_keys == {K1, K2}


def test_unverified_coderabbit_completes_the_round_with_its_reason():
    rnd, clock = new_round()
    observe(rnd, cr=PAUSED)
    clock.advance(SETTLE_SECONDS)
    out = observe(rnd, cr=PAUSED)
    assert out.status is RoundStatus.COMPLETE
    assert (rnd.state.coderabbit_state, rnd.state.coderabbit_reason) == ("unverified", PAUSED.reason)


def test_missing_required_check_ends_the_round():
    rnd, _ = new_round()
    missing = evaluate_required_checks(["test"], [], [], seconds_since_push=600, appear_timeout=600)
    out = observe(rnd, c=missing)
    assert out.status is RoundStatus.CHECK_MISSING and "test" in out.reason
    assert observe(rnd).status is RoundStatus.CHECK_MISSING  # terminal


# --- deadlines in supervised seconds (R25) -----------------------------------------------


def test_round_times_out_after_round_timeout_supervised_seconds():
    rnd, clock = new_round()
    observe(rnd, codex=False)
    clock.advance(TIMEOUT - 1)
    assert observe(rnd, codex=False).status is RoundStatus.WAITING
    clock.advance(1)
    out = observe(rnd, codex=False)
    assert out.status is RoundStatus.TIMED_OUT
    assert out.waiting_on == ("Codex review for the head",)


def test_sleep_is_not_supervised_time_but_is_recorded():
    rnd, clock = new_round()
    clock.advance(10)
    rnd.tick()
    clock.sleep_mac(3 * 3600)  # the Mac slept: wall clock moved, monotonic did not
    clock.advance(5)
    rnd.tick()
    assert rnd.state.supervised_seconds == 15
    assert len(rnd.state.sleep_gaps) == 1 and rnd.state.sleep_gaps[0][1] == pytest.approx(3 * 3600)
    assert rnd.wall_elapsed() == pytest.approx(3 * 3600 + 15)


def test_restart_does_not_count_downtime():
    rnd, clock = new_round()
    clock.advance(20 * 60)
    observe(rnd, codex=False)
    saved = json.loads(json.dumps(rnd.state.to_dict()))  # what store.py persists
    clock.advance(5 * 3600)  # gaffer stopped (or crashed) for five hours
    resumed = Round(RoundState.from_dict(saved), clock=clock, round_timeout=TIMEOUT)
    clock.advance(9 * 60)
    assert observe(resumed, codex=False).status is RoundStatus.WAITING
    assert resumed.state.supervised_seconds == 29 * 60
    clock.advance(60)
    assert observe(resumed, codex=False).status is RoundStatus.TIMED_OUT


def test_state_round_trips():
    rnd, clock = new_round()
    observe(rnd, keys={K1})
    clock.advance(SETTLE_SECONDS)
    observe(rnd, keys={K1})
    back = RoundState.from_dict(json.loads(json.dumps(rnd.state.to_dict())))
    assert back == rnd.state


# --- late items open seq+1 on the same head -------------------------------------------------


def test_late_items_open_round_seq_plus_one_with_a_fresh_timer():
    rnd, clock = new_round()
    observe(rnd, keys={K1})
    clock.advance(SETTLE_SECONDS)
    assert observe(rnd, keys={K1}).status is RoundStatus.COMPLETE
    clock.advance(25 * 60)  # writer resumes, then a reply arrives on the same head
    processed = {K1}
    assert rnd.late_keys({K1}, processed) == frozenset()
    late = rnd.late_keys({K1, K3}, processed)
    assert late == {K3}
    nxt = rnd.open_late_round(late)
    assert (nxt.state.head_sha, nxt.state.seq, nxt.state.supervised_seconds) == ("h1", 2, 0.0)
    assert nxt.state.seconds_since_push == pytest.approx(25 * 60 + SETTLE_SECONDS)
    assert observe(nxt, keys={K3}).status is RoundStatus.WAITING
    clock.advance(SETTLE_SECONDS)
    out = observe(nxt, keys={K3})
    assert out.status is RoundStatus.COMPLETE and out.packet_keys == {K3}


def test_no_late_round_without_late_items():
    rnd, _ = new_round()
    with pytest.raises(ValueError):
        rnd.open_late_round(set())
    assert rnd.late_keys({K1}, set()) == frozenset()  # round not complete yet


# --- poll_round over a fake GitHub with the sandbox fixtures --------------------------------------


HEAD = "73d64291eb50563707fca6819c70cfe4d01d4c44"


def repo_config(**kw) -> RepoConfig:
    base = dict(
        team_key="EXP", path=Path("/tmp/x"), github="mfullbrook/gaffer-coderabbit-sandbox", base="main",
        ecosystems=("python",), test_command="pytest", junit_path="junit.xml", runner="pytest", test_dirs=("tests",),
        harness_paths=(), forbidden_paths=(), install_command=None, required_checks=("test",), skippable_checks=(),
        allow_no_ci=False, extra_hosts=(),
    )
    base.update(kw)
    return RepoConfig(**base)


def sandbox_github(statuses):
    check_run = load("ci_failed_check/check_run.json")
    return FakeGitHub(
        pull={"head": {"sha": HEAD}, "state": "open", "draft": False},
        statuses={HEAD: statuses},
        check_runs={HEAD: [check_run]},
        issue_comments=[load("mark_comment/issue_comment.json"), load("round_marker_comment/issue_comment.json")],
    )


def test_poll_round_end_to_end_on_fixture_data():
    history = load("completion_signal/status_history.json")[HEAD]
    pending = [s for s in history if s["state"] == "pending"]
    gh = sandbox_github(pending)
    clock = FakeClock()
    rnd = Round.open(HEAD, clock=clock, round_timeout=TIMEOUT)
    kw = dict(repo=repo_config(), pr_number=1, operator_login="mfullbrook", processed_keys=set(), codex_recorded=True)

    out, obs = poll_round(rnd, gh, **kw)
    assert out.status is RoundStatus.WAITING
    assert obs.coderabbit.state is CodeRabbitState.PENDING
    assert [i.item_id for i in obs.admission.admitted] == ["6286685810"]  # Mark's comment, not gaffer's round comment

    gh.data["statuses"][HEAD] = history  # Review completed
    clock.advance(180)
    out, obs = poll_round(rnd, gh, **kw)
    assert out.status is RoundStatus.WAITING and obs.coderabbit.verified
    clock.advance(SETTLE_SECONDS)
    out, obs = poll_round(rnd, gh, **kw)
    assert out.status is RoundStatus.COMPLETE
    assert [v.name for v in out.checks.failed] == ["test"]
    assert out.packet_keys == obs.admission.keys
    assert set(gh.calls) >= {"pull", "statuses", "check_runs", "reviews", "review_comments", "issue_comments", "review_threads"}


def test_processed_items_are_not_pending():
    gh = sandbox_github(load("completion_signal/status_history.json")[HEAD])
    rnd = Round.open(HEAD, clock=FakeClock(), round_timeout=TIMEOUT)
    _, first = poll_round(rnd, gh, repo=repo_config(), pr_number=1, operator_login="mfullbrook", processed_keys=set(), codex_recorded=True)
    _, again = poll_round(rnd, gh, repo=repo_config(), pr_number=1, operator_login="mfullbrook", processed_keys=first.admission.keys, codex_recorded=True)
    assert again.pending_keys == frozenset()


def test_a_failed_poll_never_counts_as_complete():
    gh = sandbox_github(load("completion_signal/status_history.json")[HEAD])
    gh.fail.add("review_threads")
    clock = FakeClock()
    rnd = Round.open(HEAD, clock=clock, round_timeout=TIMEOUT)
    for _ in range(3):
        with pytest.raises(GitHubError):
            poll_round(rnd, gh, repo=repo_config(), pr_number=1, operator_login="mfullbrook", processed_keys=set(), codex_recorded=True)
        clock.advance(SETTLE_SECONDS)
    assert rnd.status is RoundStatus.WAITING
    assert rnd.state.supervised_seconds == 2 * SETTLE_SECONDS  # gaffer was supervising; time still counts


def test_coderabbit_never_started_uses_coderabbit_appear_timeout():
    gh = sandbox_github([])
    clock = FakeClock()
    rnd = Round.open(HEAD, clock=clock, round_timeout=TIMEOUT)
    kw = dict(repo=repo_config(coderabbit_appear_timeout=300), pr_number=1, operator_login="mfullbrook", processed_keys=set(), codex_recorded=True)
    _, obs = poll_round(rnd, gh, **kw)
    assert obs.coderabbit.state is CodeRabbitState.PENDING
    clock.advance(300)
    _, obs = poll_round(rnd, gh, **kw)
    assert obs.coderabbit.state is CodeRabbitState.UNVERIFIED
    assert "never started" in obs.coderabbit.reason


# --- UrllibGitHubReader with a fake opener ------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers=None):
        super().__init__(body)
        self.headers = _Headers(headers or {})


class _Headers(dict):
    def items(self):
        return super().items()


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_reader_paginates_and_keeps_the_token_off_redirects():
    opener = FakeOpener(
        [
            FakeResponse(b'[{"id": 1}]', {"Link": '<https://api.github.com/repositories/9/issues/1/comments?page=2>; rel="next"'}),
            FakeResponse(b'[{"id": 2}]'),
        ]
    )
    gh = UrllibGitHubReader(Secret("ghp_example"), opener=opener)
    assert gh.issue_comments("o/r", 1) == [{"id": 1}, {"id": 2}]
    first = opener.requests[0]
    assert first.full_url == "https://api.github.com/repos/o/r/issues/1/comments?per_page=100"
    assert first.unredirected_hdrs["Authorization"] == "Bearer ghp_example"
    assert "Authorization" not in first.headers  # unredirected headers are not copied to a redirect target
    assert opener.requests[1].full_url.endswith("page=2")


def test_reader_check_runs_unwraps_and_graphql_paginates():
    page = lambda nodes, nxt: FakeResponse(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": nxt, "endCursor": "c1" if nxt else None}, "nodes": nodes}}}}}).encode())
    opener = FakeOpener([
        FakeResponse(b'{"total_count": 1, "check_runs": [{"id": 5}]}'),
        page([{"id": "T1"}], True),
        page([{"id": "T2"}], False),
    ])
    gh = UrllibGitHubReader(Secret("t"), opener=opener)
    assert gh.check_runs("o/r", "abc") == [{"id": 5}]
    assert [t["id"] for t in gh.review_threads("o/r", 1)] == ["T1", "T2"]
    second = json.loads(opener.requests[2].data)
    assert second["variables"] == {"owner": "o", "name": "r", "number": 1, "after": "c1"}


def test_reader_errors_are_github_errors_without_the_token():
    opener = FakeOpener([
        urllib.error.HTTPError("https://api.github.com/repos/o/r/pulls/1", 502, "Bad Gateway", {}, None),
        FakeResponse(b'{"errors": [{"message": "rate limited"}]}'),
    ])
    gh = UrllibGitHubReader(Secret("secret-token"), opener=opener)
    with pytest.raises(GitHubError) as e:
        gh.pull("o/r", 1)
    assert "502" in str(e.value) and "secret-token" not in str(e.value)
    with pytest.raises(GitHubError):
        gh.review_threads("o/r", 1)
