"""CodeRabbit completion and admission (R12 as amended by R27), one test per
directory in tests/fixtures/coderabbit/, each checked against its expected.json."""

import copy
import hashlib

import pytest

from gaffer.config import DEFAULT_CODERABBIT_APPEAR_TIMEOUT
from gaffer.reviews.checks import CheckState, collect_check_evidence, evaluate_required_checks
from gaffer.reviews.coderabbit import (
    NEVER_STARTED,
    NORMALIZE_PATTERN,
    REVIEW_COMPLETED,
    STATUS_CONTEXT,
    CodeRabbitState,
    IncompleteData,
    ItemKind,
    Source,
    admit,
    coderabbit_status,
    content_hash,
    parse_review_body,
    pr_body_outside_release_notes,
)
from reviews_fakes import CODERABBIT, FakeGitHub, load

OPERATOR = "mfullbrook"
T = DEFAULT_CODERABBIT_APPEAR_TIMEOUT


def status(state, description, at="2026-10-20T08:00:00Z", creator="coderabbitai[bot]", id=1):
    return {"id": id, "state": state, "description": description, "context": "CodeRabbit", "created_at": at, "creator": {"login": creator}}


def cr(statuses, since=60):
    return coderabbit_status(statuses, seconds_since_push=since, appear_timeout=T)


def admit_with(**kw):
    kw.setdefault("head_sha", "7cbe02269a8201320c163fcd789dd478b7cf932a")
    return admit(operator_login=OPERATOR, **kw)


def covers(name):
    def mark(fn):
        FIXTURE_TESTS[name] = fn
        return fn

    return mark


FIXTURE_TESTS: dict = {}


# --- one test per fixture directory ------------------------------------------------------


@covers("ci_failed_check")
def test_ci_failed_check():
    exp = load("ci_failed_check/expected.json")
    check_run = load("ci_failed_check/check_run.json")
    tail = (CODERABBIT / "ci_failed_check/job_log_tail200.txt").read_text()
    # The raw log has 223 lines and ANSI codes (Experiment 2 section 7); the fixture is the stripped tail.
    early = "".join(f"2026-10-20T08:15:51.{i:07d}Z \x1b[36;1mrunner setup {i}\x1b[0m\n" for i in range(23))
    coloured = "".join(f"\x1b[31m{ln[:-1]}\x1b[0m\n" if "FAILED" in ln else ln for ln in tail.splitlines(keepends=True))
    gh = FakeGitHub(annotations={check_run["id"]: load("ci_failed_check/annotations.json")}, logs={check_run["id"]: (early + coloured).encode()})

    ev = collect_check_evidence(gh, exp["source"]["repo"], check_run)

    item = exp["item"]
    assert (ev.name, ev.check_run_id, ev.conclusion) == (item["name"], item["check_run_id"], item["conclusion"])
    assert ev.title is None and ev.summary is None  # null for Actions; only the log carries the failure
    assert len(ev.annotations) == item["annotations"]
    assert ev.log_tail == tail
    assert len(ev.log_tail.splitlines()) == item["log_tail_lines"]
    assert len(ev.log_tail.encode()) == item["log_tail_bytes"]
    assert "\x1b" not in ev.log_tail
    assert "FAILED tests/test_inventory.py::test_last_n" in ev.log_tail
    verdict = evaluate_required_checks(["test"], [], [check_run], seconds_since_push=60, appear_timeout=600).verdicts[0]
    assert exp["verdict"] == "admit" and verdict.state is CheckState.FAIL  # a failed required check becomes a packet item


@covers("completion_signal")
def test_completion_signal():
    exp = load("completion_signal/expected.json")
    assert exp["signal"] == {
        "endpoint": "GET /repos/{o}/{r}/commits/{head}/statuses",
        "context": STATUS_CONTEXT,
        "state": "success",
        "description": REVIEW_COMPLETED,
    }
    history = load("completion_signal/status_history.json")
    for case in exp["cases"]:
        if "status_sequence" in case:
            statuses = [status(s, d, at=f"2026-10-20T08:00:0{i}Z", id=i) for i, (s, d) in enumerate(case["status_sequence"])]
        elif case.get("synthetic"):
            statuses = [status(*case["status"])]
        elif "file" in case:
            combined = load(f"completion_signal/{case['file']}")
            # The combined-status endpoint has no creator, so gaffer reads the status list instead.
            assert all("creator" not in s for s in combined["statuses"])
            snap_time = "08:15:53Z"
            statuses = [s for s in history[case["head"]] if s["created_at"][11:] <= snap_time]
            assert [(s["state"], s["description"]) for s in statuses] == [tuple(case["status"])]
        elif "at" in case:
            statuses = [s for s in history[case["head"]] if s["created_at"][11:] <= case["at"]]
            newest = max(statuses, key=lambda s: s["created_at"])
            assert [newest["state"], newest["description"]] == case["status"]
        else:
            statuses = history[case["head"]]
            assert statuses == case["statuses"] == []

        got = cr(statuses)
        if case["verdict"] == "complete":
            assert got.state is CodeRabbitState.COMPLETE, case
        elif case["verdict"] == "not_complete":
            assert got.state is not CodeRabbitState.COMPLETE, case
            if statuses and statuses[0]["state"] == "success":  # skipped / paused: unverified at once (R27)
                assert got.state is CodeRabbitState.UNVERIFIED and statuses[0]["description"] in got.reason
            else:
                assert got.state is CodeRabbitState.PENDING
        else:
            assert case["verdict"] == "no_signal"
            assert got.state is CodeRabbitState.PENDING
            late = cr(statuses, since=T)
            assert late.state is CodeRabbitState.UNVERIFIED and late.reason.startswith(NEVER_STARTED)


@covers("draft_pr")
def test_draft_pr():
    exp = load("draft_pr/expected.json")
    statuses = load("draft_pr/statuses.json")
    got = cr(statuses)
    assert exp["verdict"] == "not_complete"
    assert got.state is CodeRabbitState.UNVERIFIED  # success state, but not "Review completed"
    assert "Review skipped: draft pull request" in got.reason
    assert load("draft_pr/pull.json")["draft"] is True
    skip_comment = load("draft_pr/issue_comment.json")
    a = admit_with(issue_comments=[skip_comment])
    assert a.verdict(skip_comment["id"]) == "ignore" and not a.admitted


@covers("inline_finding_open_thread")
def test_inline_finding_open_thread():
    exp = load("inline_finding_open_thread/expected.json")
    comment = load("inline_finding_open_thread/review_comment.json")
    thread = load("inline_finding_open_thread/review_thread.graphql.json")
    review = load("inline_finding_open_thread/review.json")
    a = admit_with(head_sha=review["commit_id"], review_comments=[comment], review_threads=[thread], reviews=[review])
    assert a.verdict(exp["item_key"]["item_id"]) == exp["verdict"] == "admit"
    [item] = a.admitted
    assert (item.source.value, item.item_id) == (exp["item_key"]["source"], str(exp["item_key"]["item_id"]))
    assert item.kind is ItemKind.INLINE_COMMENT
    assert (item.path, item.line_start, item.line_end) == ("app/inventory.py", 25, 25)
    assert item.content_hash == content_hash(comment["body"])
    assert a.verdict(review["id"]) == "ignore"  # "Actionable comments posted: 3" body: its findings are the inline comments


@covers("mark_comment")
def test_mark_comment():
    exp = load("mark_comment/expected.json")
    comment = load("mark_comment/issue_comment.json")
    a = admit_with(issue_comments=[comment])
    assert a.verdict(comment["id"]) == exp["verdict"] == "admit"
    assert a.admitted[0].source is Source.OPERATOR
    assert a.admitted[0].body == comment["body"]


@covers("outdated_comment")
def test_outdated_comment():
    exp = load("outdated_comment/expected.json")
    comment = load("outdated_comment/review_comment.json")
    thread = load("outdated_comment/review_thread.graphql.json")
    assert comment["line"] is None and thread["isOutdated"] and not thread["isResolved"]
    a = admit_with(review_comments=[comment], review_threads=[thread])
    assert a.verdict(comment["id"]) == exp["verdict"] == "context"
    assert not a.admitted


@covers("resolution_marker_edit")
def test_resolution_marker_edit():
    exp = load("resolution_marker_edit/expected.json")
    assert NORMALIZE_PATTERN == exp["normalization"]
    before = load("resolution_marker_edit/review_comment_before.json")
    after = load("resolution_marker_edit/review_comment_after.json")
    raw = lambda c: hashlib.sha256(c["body"].encode()).hexdigest()
    assert (raw(before) != raw(after)) is exp["raw_hash_changed"]
    assert (content_hash(before["body"]) == content_hash(after["body"])) is exp["normalized_hash_equal"]
    edits = load("resolution_marker_edit/review_comment_edits.graphql.json")["userContentEdits"]["nodes"]
    assert len({content_hash(e["diff"]) for e in edits}) == 1
    # The same comment on an open thread before and after the edit has one item key: no new item.
    thread = {"isResolved": False, "isOutdated": False, "line": 19, "path": "app/inventory.py",
              "comments": {"nodes": [{"databaseId": before["id"], "author": {"login": "coderabbitai"}}]}}
    k1 = admit_with(review_comments=[before], review_threads=[thread]).keys
    k2 = admit_with(review_comments=[after], review_threads=[thread]).keys
    assert k1 == k2 and len(k1) == 1
    assert exp["verdict"] == "ignore"


@covers("resolved_thread")
def test_resolved_thread():
    exp = load("resolved_thread/expected.json")
    by_mark = load("resolved_thread/review_comment_resolved_by_mark.json")
    by_cr_comment = load("resolution_marker_edit/review_comment_after.json")  # the comment in thread PRRT_kwDOVP5T9s6uP7si
    threads = {
        "PRRT_kwDOVP5T9s6uQFa8": (load("resolved_thread/review_thread_resolved_by_mark.graphql.json"), by_mark),
        "PRRT_kwDOVP5T9s6uP7si": (load("resolved_thread/review_thread_resolved_by_coderabbit.graphql.json"), by_cr_comment),
    }
    for case in exp["cases"]:
        thread, comment = threads[case["thread"]]
        assert thread["comments"]["nodes"][0]["databaseId"] == comment["id"]
        assert thread["resolvedBy"]["login"] == case["resolved_by"] and thread["isOutdated"] is case["outdated"]
        a = admit_with(review_comments=[comment], review_threads=[thread])
        assert a.verdict(comment["id"]) == case["verdict"] == "ignore"
        assert case["resolved_by"] in a.ignored[0].reason


@covers("review_body_actionable")
def test_review_body_actionable():
    exp = load("review_body_actionable/expected.json")
    open_thread = load("inline_finding_open_thread/review_thread.graphql.json")
    resolved = copy.deepcopy(open_thread) | {"isResolved": True, "resolvedBy": {"login": "coderabbitai[bot]"}}
    # Thread state at each review: line-25 thread open in rounds 1-2, fixed and self-resolved by round 3.
    thread_at = {5582416328: open_thread, 5582496841: open_thread, 5582638738: resolved}
    for case in exp["cases"]:
        review = load(f"review_body_actionable/{case['file']}")
        assert review["id"] == case["review_id"]
        a = admit_with(head_sha=review["commit_id"], reviews=[review], review_threads=[thread_at[review["id"]]])
        body_items = [i for i in a.admitted if i.kind in (ItemKind.OUTSIDE_DIFF, ItemKind.NITPICK)]
        if case["verdict"] == "ignore":
            assert not body_items and a.verdict(review["id"]) == "ignore"
        elif case["verdict"] == "admit_deduplicate":
            [entry] = parse_review_body(review["body"]).entries
            assert (entry.kind, entry.path, entry.line_start) == (ItemKind.OUTSIDE_DIFF, case["duplicate_of"]["path"], 25)
            assert not body_items
            [dup] = [d for d in a.ignored if d.item_id == f"{review['id']}/outside_diff/1"]
            assert str(case["duplicate_of"]["review_comment_id"]) in dup.reason
        else:
            assert case["verdict"] == "admit"
            [item] = body_items
            assert (item.kind, item.path, item.line_start, item.line_end) == (ItemKind.OUTSIDE_DIFF, "app/inventory.py", 24, 26)
            assert "Clamp the slice start" in item.body
            assert "Actionable comments posted" not in review["body"]  # parsed by section, not by header


@covers("round_marker_comment")
def test_round_marker_comment():
    exp = load("round_marker_comment/expected.json")
    comment = load("round_marker_comment/issue_comment.json")
    sent = (CODERABBIT / "round_marker_comment/sent_body.txt").read_text()
    assert (comment["body"] == sent) is exp["byte_identical"]
    assert exp["marker_line"] in comment["body"].splitlines()
    assert comment["user"]["login"] == OPERATOR  # same login as mark_comment: excluded by marker, not author
    a = admit_with(issue_comments=[comment])
    assert a.verdict(comment["id"]) == exp["verdict"] == "ignore"
    assert "gaffer-round" in a.ignored[0].reason


@covers("status_edit")
def test_status_edit():
    exp = load("status_edit/expected.json")
    versions = load("status_edit/issue_comment_status_versions.graphql.json")
    assert [v["editedAt"] for v in versions] == [c["edited_at"] for c in exp["cases"]]
    for i, v in enumerate(versions):
        comment = {"id": 6286245831, "user": {"login": "coderabbitai[bot]"}, "body": v["diff"]}
        a = admit_with(issue_comments=[comment])
        assert a.verdict(comment["id"]) == exp["cases"][i]["verdict"] == "ignore"
        assert not a.admitted


@covers("stranger_comment")
def test_stranger_comment():
    exp = load("stranger_comment/expected.json")
    comment = load("stranger_comment/issue_comment.json")
    a = admit_with(issue_comments=[comment])
    assert a.verdict(comment["id"]) == exp["verdict"] == "ignore"
    assert "example-stranger" in a.ignored[0].reason  # listed in the report


@covers("walkthrough_edit")
def test_walkthrough_edit():
    exp = load("walkthrough_edit/expected.json")
    summary = load("walkthrough_edit/issue_comment.json")
    edits = load("walkthrough_edit/issue_comment_edits.graphql.json")["userContentEdits"]["nodes"]
    assert len(edits) == exp["cases"][0]["edits"]
    for body in [summary["body"]] + [e["diff"] for e in edits]:
        a = admit_with(issue_comments=[{"id": summary["id"], "user": summary["user"], "body": body}])
        assert a.verdict(summary["id"]) == "ignore" and not a.admitted
    # PR body: CodeRabbit's release-notes edits are ignored; the text outside its markers is stable.
    body_edits = load("walkthrough_edit/pull_body_edits.graphql.json")["userContentEdits"]["nodes"]
    outside = {pr_body_outside_release_notes(e["diff"]) for e in body_edits}
    assert outside == {"Sandbox PR for gaffer Experiment 2 (CodeRabbit capture). Not for merge.\n\ngaffer-job: exp2"}
    assert len({e["diff"] for e in body_edits}) == 4  # the raw body changed on every CodeRabbit edit
    assert "gaffer-job: exp2" in pr_body_outside_release_notes(load("walkthrough_edit/pull.json")["body"])


def test_every_fixture_directory_has_a_test():
    dirs = {p.name for p in CODERABBIT.iterdir() if p.is_dir()}
    assert dirs == set(FIXTURE_TESTS)
    for d in dirs:
        assert (CODERABBIT / d / "expected.json").is_file()


# --- rules beyond the fixtures ------------------------------------------------------------


def test_coderabbit_reply_in_a_thread_is_context_attached_to_its_item():
    comment = load("inline_finding_open_thread/review_comment.json")
    thread = load("inline_finding_open_thread/review_thread.graphql.json")
    reply = {"id": 99, "in_reply_to_id": comment["id"], "user": {"login": "coderabbitai[bot]"}, "body": "You're right, thanks!"}
    a = admit_with(review_comments=[comment, reply], review_threads=[thread])
    assert a.verdict(99) == "context"
    [item] = a.admitted
    assert item.context == ("You're right, thanks!",)
    assert item.key == admit_with(review_comments=[comment], review_threads=[thread]).admitted[0].key  # no new item


def test_operator_reply_in_an_open_thread_is_admitted_and_login_is_case_insensitive():
    comment = load("inline_finding_open_thread/review_comment.json")
    thread = load("inline_finding_open_thread/review_thread.graphql.json")
    reply = {"id": 100, "in_reply_to_id": comment["id"], "user": {"login": "MFullbrook"}, "body": "Please also cover n=0."}
    a = admit(head_sha="x", operator_login="mfullbrook", review_comments=[comment, reply], review_threads=[thread])
    assert a.verdict(100) == "admit"
    assert {i.source for i in a.admitted} == {Source.CODERABBIT, Source.OPERATOR}


def test_inline_comment_without_its_thread_is_incomplete_data():
    comment = load("inline_finding_open_thread/review_comment.json")
    with pytest.raises(IncompleteData):
        admit_with(review_comments=[comment], review_threads=[])


def test_review_body_for_an_earlier_head_is_ignored():
    review = load("review_body_actionable/review_round3_outside_diff_only.json")
    a = admit_with(head_sha="0" * 40, reviews=[review])
    assert a.verdict(review["id"]) == "ignore" and not a.admitted


def test_status_from_another_creator_and_unrecognized_descriptions_are_not_completion():
    spoof = status("success", "Review completed", creator="someone-else")
    assert cr([spoof]).state is CodeRabbitState.PENDING
    odd = cr([status("success", "Review finished")])
    assert odd.state is CodeRabbitState.PENDING and odd.unrecognized
    assert "'Review finished'" in odd.reason  # logged verbatim
    assert cr([status("success", "Review approved")]).state is CodeRabbitState.PENDING


def test_never_started_names_the_missing_check_suite():
    got = coderabbit_status([], seconds_since_push=T, appear_timeout=T, check_suite_present=False)
    assert got.reason == NEVER_STARTED + " (no coderabbitai check suite on the head)"


CLASSIC_NITPICKS = """**Actionable comments posted: 0**

<details>
<summary>🧹 Nitpick comments (2)</summary><blockquote>

<details>
<summary>app/inventory.py (2)</summary><blockquote>

`9-11`: **Prefer a tuple for the default.**

Using a tuple avoids accidental mutation.

---

`25`: **Name the magic number.**

Extract `1` into a constant.

</blockquote></details>

</blockquote></details>

<details>
<summary>📜 Review details</summary>

Run configuration and commits.

</details>
"""

NOT_REVIEWED = """**Actionable comments posted: 0**

<details>
<summary>⛔ Files not reviewed due to moderation or processing errors (1)</summary>

* `app/huge.py`

</details>
"""


def test_classic_nitpick_layout_is_parsed_per_entry():
    """Synthetic: the Phase 1 layout (per-file <details> with `lines`: entries); no Phase 1 text."""
    entries = parse_review_body(CLASSIC_NITPICKS).entries
    assert [(e.kind, e.index, e.path, e.line_start, e.line_end) for e in entries] == [
        (ItemKind.NITPICK, 1, "app/inventory.py", 9, 11),
        (ItemKind.NITPICK, 2, "app/inventory.py", 25, 25),
    ]
    assert "Prefer a tuple" in entries[0].text and "Name the magic number" not in entries[0].text
    a = admit_with(head_sha="h", reviews=[{"id": 7, "commit_id": "h", "user": {"login": "coderabbitai[bot]"}, "body": CLASSIC_NITPICKS}])
    assert [i.item_id for i in a.admitted] == ["7/nitpick/1", "7/nitpick/2"]


def test_files_not_reviewed_section_is_named():
    a = admit_with(head_sha="h", reviews=[{"id": 8, "commit_id": "h", "user": {"login": "coderabbitai[bot]"}, "body": NOT_REVIEWED}])
    assert a.not_reviewed_files == ("app/huge.py",)
    assert not a.admitted


def test_operator_review_body_is_admitted_unless_it_is_a_round_comment():
    reviews = [
        {"id": 1, "user": {"login": OPERATOR}, "body": "Rename last_n to tail."},
        {"id": 2, "user": {"login": OPERATOR}, "body": "summary\ngaffer-round: job/abc/1"},
        {"id": 3, "user": {"login": OPERATOR}, "body": ""},
    ]
    a = admit_with(reviews=reviews)
    assert (a.verdict(1), a.verdict(2), a.verdict(3)) == ("admit", "ignore", None)


def test_marker_must_start_a_line():
    a = admit_with(issue_comments=[{"id": 5, "user": {"login": OPERATOR}, "body": "see the gaffer-round: marker docs"}])
    assert a.verdict(5) == "admit"
