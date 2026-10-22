"""Packets (R12, R13 as amended by R27, R15, R24): contents, caps cut from the end, secret scan."""

import json

import pytest

from gaffer.reviews.checks import CHECK_CAP_BYTES, MAX_ANNOTATIONS, CheckEvidence, collect_check_evidence, log_tail
from gaffer.reviews.coderabbit import FeedbackItem, ItemKind, Source, admit
from gaffer.reviews.junit import CriterionMap, ExpectedTest, Label, match_inventory, parse_junit
from gaffer.reviews.packet import PACKET_CAP_BYTES, SecretDetected, build_packet, render_check
from reviews_fakes import CODERABBIT, JUNIT, FakeGitHub, FakeScanner, load


def item(i: int, body: str = "Fix the off-by-one.") -> FeedbackItem:
    return FeedbackItem(Source.CODERABBIT, str(i), f"h{i}", ItemKind.INLINE_COMMENT, "coderabbitai[bot]", body, "app/x.py", 3, 5)


def evidence(i: int, log: str) -> CheckEvidence:
    return CheckEvidence(f"check{i}", 1000 + i, "failure", None, None, (), 0, log_tail(log))


def big_log(n_lines: int = 2000, width: int = 100) -> str:
    return "".join(f"line {k:05d} " + "x" * width + "\n" for k in range(n_lines))


def fixture_evidence() -> CheckEvidence:
    check_run = load("ci_failed_check/check_run.json")
    gh = FakeGitHub(
        annotations={check_run["id"]: load("ci_failed_check/annotations.json")},
        logs={check_run["id"]: (CODERABBIT / "ci_failed_check/job_log_tail200.txt").read_bytes()},
    )
    return collect_check_evidence(gh, "o/r", check_run)


def failing_tests():
    cmap = CriterionMap.from_tester_output(json.loads((JUNIT / "pytest/tester.json").read_text()))
    return match_inventory(cmap, parse_junit("pytest", (JUNIT / "pytest/base_typo.xml").read_bytes())).failing


# --- contents -----------------------------------------------------------------------------


def test_packet_holds_items_then_failing_tests_then_failed_checks():
    p = build_packet(head_sha="73d64291eb50", seq=2, scanner=FakeScanner(), items=[item(1)], failing_tests=failing_tests(), failed_checks=[fixture_evidence()])
    sources = [k[0] for k in p.included_keys]
    assert sources == ["coderabbit", "test", "test", "test", "check"]
    assert p.text.startswith("# gaffer packet: head 73d64291eb50, round 2")
    assert "app/x.py:3-5" in p.text
    assert "NameError: name 'shuot' is not defined" in p.text
    assert "FAILED tests/test_inventory.py::test_last_n" in p.text
    assert "Process completed with exit code 1." in p.text  # annotations
    assert not p.omitted_keys


def test_admitted_thread_replies_appear_as_context():
    comment = load("inline_finding_open_thread/review_comment.json")
    thread = load("inline_finding_open_thread/review_thread.graphql.json")
    reply = {"id": 9, "in_reply_to_id": comment["id"], "user": {"login": "coderabbitai[bot]"}, "body": "Confirmed."}
    a = admit(head_sha="x", operator_login="mfullbrook", review_comments=[comment, reply], review_threads=[thread])
    p = build_packet(head_sha="x", seq=1, scanner=FakeScanner(), items=a.admitted)
    assert "thread reply (context):\nConfirmed." in p.text
    assert "auto-generated comment by CodeRabbit" not in p.text  # normalized body


def test_failing_test_output_is_bounded():
    entry = failing_tests()[0]
    huge = entry.__class__(entry.test, entry.outcome, entry.case.__class__(**{**entry.case.__dict__, "text": "y" * 100_000}) if entry.case else None, entry.collection_error, entry.detail)
    p = build_packet(head_sha="x", seq=1, scanner=FakeScanner(), failing_tests=[huge])
    assert p.size < 6 * 1024


# --- per-check cap: 16 KB, cut from the end (R13, R27) --------------------------------------


def test_log_tail_strips_ansi_then_keeps_last_200_lines():
    raw = "".join(f"\x1b[32mline {k}\x1b[0m\n" for k in range(500))
    tail = log_tail(raw.encode())
    lines = tail.splitlines()
    assert len(lines) == 200 and lines[0] == "line 300" and lines[-1] == "line 499"
    assert "\x1b" not in tail


def test_log_tail_cap_keeps_the_newest_lines():
    tail = log_tail(big_log(200, 200))
    assert len(tail.encode()) <= CHECK_CAP_BYTES
    assert tail.endswith("line 00199 " + "x" * 200 + "\n")
    assert tail.startswith("line ")  # cut on a line boundary


def test_rendered_check_fits_16kb_and_keeps_the_end_of_the_log():
    e = CheckEvidence("test", 1, "failure", "Tests failed", "3 failed", tuple({"path": "a.py", "start_line": k, "annotation_level": "failure", "message": "m" * 50} for k in range(50)), 50, log_tail(big_log(200, 200)))
    text = render_check(e)
    assert len(text.encode()) <= CHECK_CAP_BYTES
    assert "Title: Tests failed" in text and "Summary: 3 failed" in text
    assert text.endswith("line 00199 " + "x" * 200 + "\n")


def test_fixture_check_fits_its_cap():
    text = render_check(fixture_evidence())
    assert len(text.encode()) <= CHECK_CAP_BYTES
    assert "1 failed, 2 passed in 0.05s" in text


def test_annotations_are_capped_at_50():
    check_run = load("ci_failed_check/check_run.json") | {"output": {"title": None, "summary": None, "annotations_count": 70}}
    many = [{"path": "a.py", "start_line": k, "annotation_level": "warning", "message": f"w{k}"} for k in range(70)]
    ev = collect_check_evidence(FakeGitHub(annotations={check_run["id"]: many}, logs={check_run["id"]: b"x\n"}), "o/r", check_run)
    assert len(ev.annotations) == MAX_ANNOTATIONS and ev.annotations_total == 70
    assert "Annotations (50 of 70)" in render_check(ev)


def test_log_fetch_failure_is_a_note_not_an_empty_log():
    check_run = load("ci_failed_check/check_run.json")
    ev = collect_check_evidence(FakeGitHub(annotations={check_run["id"]: []}), "o/r", check_run)
    assert ev.log_tail is None and "HTTP 404" in ev.log_note
    assert "(no log: job log unavailable" in render_check(ev)


# --- packet cap: 48 KB, cut from the end ------------------------------------------------------


def test_packet_cap_cuts_from_the_end_and_lists_omitted_items():
    checks = [evidence(i, big_log(200, 200)) for i in range(5)]  # ~16 KB each
    p = build_packet(head_sha="h", seq=1, scanner=FakeScanner(), items=[item(1), item(2)], failed_checks=checks)
    assert p.size <= PACKET_CAP_BYTES
    assert [k[1] for k in p.included_keys][:2] == ["1", "2"]  # review items first, never cut
    assert p.omitted_keys and all(k[0] == "check" for k in p.omitted_keys)
    assert set(p.included_keys).isdisjoint(p.omitted_keys)
    assert len(p.included_keys) + len(p.omitted_keys) == 7
    assert "omitted by the 48 KB packet cap" in p.text
    assert p.items[-1].truncated  # the check that crossed the cap is cut, not dropped


def test_small_packet_is_not_cut():
    p = build_packet(head_sha="h", seq=1, scanner=FakeScanner(), items=[item(i) for i in range(5)])
    assert not p.omitted_keys and not any(i.truncated for i in p.items)


# --- secret scan before text enters the packet (R15) ----------------------------------------------


def test_every_item_is_scanned_before_it_enters_the_packet():
    scanner = FakeScanner()
    p = build_packet(head_sha="h", seq=1, scanner=scanner, items=[item(1)], failing_tests=failing_tests()[:1], failed_checks=[fixture_evidence()])
    assert len(scanner.seen) == 3
    for text in scanner.seen:
        assert text in p.text


def test_secret_hit_raises_with_the_item_key_and_redacted_findings():
    scanner = FakeScanner("ghp_")
    with pytest.raises(SecretDetected) as e:
        build_packet(head_sha="h", seq=1, scanner=scanner, items=[item(1), item(2, "token ghp_abcdef0123456789")])
    assert e.value.item_key[:2] == ("coderabbit", "2")
    assert "ghp_abcdef" not in str(e.value)
    assert e.value.findings[0].rule == "generic-api-key"


def test_secret_in_a_log_tail_is_caught_even_if_the_packet_cap_would_cut_it():
    checks = [evidence(i, big_log(200, 200)) for i in range(4)]
    checks.append(evidence(9, "AKIAIOSFODNN7EXAMPLE\n"))
    with pytest.raises(SecretDetected) as e:
        build_packet(head_sha="h", seq=1, scanner=FakeScanner("AKIA"), failed_checks=checks)
    assert e.value.item_key[:2] == ("check", "h/1009")


def test_verify_repair_packet_holds_only_failing_tests():
    """R24: a failed VERIFY resumes the writer with failing tester and guard tests only."""
    p = build_packet(head_sha="local", seq=0, scanner=FakeScanner(), failing_tests=failing_tests())
    assert {k[0] for k in p.included_keys} == {"test"}
    assert "test_word_count_counts_whitespace_separated_words" in p.text
    assert "file-level error" in p.text
