"""R31: Codex review findings are packet items, and failing-test, failed-check and
Codex item ids carry the head SHA (T6 report item 2). No network: fakes only."""

import json

import pytest

from gaffer.children import ReviewFinding
from gaffer.reviews.checks import CheckEvidence, log_tail
from gaffer.reviews.coderabbit import CodexReview, FeedbackItem, ItemKind, Source, admit, codex_items
from gaffer.reviews.junit import CriterionMap, match_inventory, parse_junit
from gaffer.reviews.packet import PACKET_CAP_BYTES, SecretDetected, build_packet, failed_check_key, failing_test_key
from gaffer.reviews.rounds import Round, RoundStatus, SETTLE_SECONDS, poll_round
from gaffer.store import ITEM_SOURCES
from reviews_fakes import JUNIT, FakeClock, FakeScanner, load
from test_rounds import HEAD, TIMEOUT, repo_config, sandbox_github

H1 = "1" * 40
H2 = "2" * 40


def finding(i: int, body: str = "Off-by-one in the loop bound.", **kw) -> ReviewFinding:
    return ReviewFinding(**{"id": f"F{i}", "severity": "major", "path": "toy/strings.py", "line": 10 + i,
                            "title": f"Finding {i}", "body": body, **kw})


def cr_item(i: int, body: str = "Fix the off-by-one.") -> FeedbackItem:
    return FeedbackItem(Source.CODERABBIT, str(i), f"h{i}", ItemKind.INLINE_COMMENT, "coderabbitai[bot]", body, "app/x.py", 3, 5)


def failing_tests():
    cmap = CriterionMap.from_tester_output(json.loads((JUNIT / "pytest/tester.json").read_text()))
    return match_inventory(cmap, parse_junit("pytest", (JUNIT / "pytest/base_typo.xml").read_bytes())).failing


def failed_check(i: int = 7, log: str = "FAILED tests/test_x.py::test_y\n") -> CheckEvidence:
    return CheckEvidence(f"check{i}", 1000 + i, "failure", None, None, (), 0, log_tail(log))


def big_log(n_lines: int = 200, width: int = 200) -> str:
    return "".join(f"line {k:05d} " + "x" * width + "\n" for k in range(n_lines))


# --- sources match the store -----------------------------------------------------------------------


def test_item_sources_are_the_stores():
    assert {s.value for s in Source} == {"coderabbit", "operator", "check", "test", "codex"}
    assert {s.value for s in Source} <= ITEM_SOURCES


# --- Codex findings as admitted items ------------------------------------------------------------


def test_codex_findings_for_the_current_head_are_admitted_items():
    a = admit(head_sha=H1, operator_login="mfullbrook", codex_reviews=[CodexReview(H1, (finding(1), finding(2)))])
    assert [i.item_id for i in a.admitted] == [f"{H1}/F1", f"{H1}/F2"]
    i = a.admitted[0]
    assert i.source is Source.CODEX and i.kind is ItemKind.CODEX_FINDING and i.author == "codex"
    assert i.key[0] == "codex" and i.path == "toy/strings.py" and i.line_start == i.line_end == 11
    assert i.body.startswith("[major] Finding 1") and "Off-by-one" in i.body
    assert a.verdict(f"{H1}/F1") == "admit"


def test_a_codex_review_for_another_head_is_ignored():
    a = admit(head_sha=H2, operator_login="mfullbrook", codex_reviews=[CodexReview(H1, (finding(1),))])
    assert a.admitted == ()
    assert a.ignored[0].source == "codex" and "earlier head" in a.ignored[0].reason


def test_findings_may_be_mappings_and_identical_duplicates_collapse():
    f = {"id": "F1", "severity": "minor", "path": None, "line": None, "title": "Name", "body": "Rename x."}
    items = codex_items(CodexReview(H1, (f, dict(f))))
    assert len(items) == 1 and items[0].path is None and items[0].line_start is None


def test_the_same_codex_finding_on_a_new_head_is_a_new_item():
    [a] = codex_items(CodexReview(H1, (finding(1),)))
    [b] = codex_items(CodexReview(H2, (finding(1),)))
    assert a.content_hash == b.content_hash and a.key != b.key
    assert a.item_id.startswith(H1) and b.item_id.startswith(H2)


def test_poll_round_admits_codex_findings_into_the_round_packet():
    gh = sandbox_github(load("completion_signal/status_history.json")[HEAD])
    clock = FakeClock()
    rnd = Round.open(HEAD, clock=clock, round_timeout=TIMEOUT)
    review = CodexReview(HEAD, (finding(1),))
    kw = dict(repo=repo_config(), pr_number=1, operator_login="mfullbrook", processed_keys=set(), codex_recorded=True,
              codex_reviews=[review])
    out, obs = poll_round(rnd, gh, **kw)
    codex_key = codex_items(review)[0].key
    assert codex_key in obs.pending_keys
    clock.advance(SETTLE_SECONDS)
    out, obs = poll_round(rnd, gh, **kw)
    assert out.status is RoundStatus.COMPLETE and codex_key in out.packet_keys
    # Once processed, the finding is no longer pending.
    _, again = poll_round(Round.open(HEAD, clock=clock, round_timeout=TIMEOUT), gh, **{**kw, "processed_keys": out.packet_keys})
    assert codex_key not in again.pending_keys


# --- head SHA in failing-test and failed-check ids ------------------------------------------------


def test_identical_failures_on_a_new_head_are_new_items():
    entry = failing_tests()[0]
    check = failed_check()
    t1, t2 = failing_test_key(H1, entry), failing_test_key(H2, entry)
    c1, c2 = failed_check_key(H1, check), failed_check_key(H2, check)
    assert t1[0] == "test" and c1[0] == "check"
    assert t1[2] == t2[2] and c1[2] == c2[2]  # same text, same hash ...
    assert t1 != t2 and c1 != c2  # ... but a different item on each head
    assert t1[1] == f"{H1}/{entry.test.identity}" and c1[1] == f"{H1}/{check.check_run_id}"
    p1 = build_packet(head_sha=H1, seq=1, scanner=FakeScanner(), failing_tests=[entry], failed_checks=[check])
    p2 = build_packet(head_sha=H2, seq=1, scanner=FakeScanner(), failing_tests=[entry], failed_checks=[check])
    assert p1.included_keys == (t1, c1) and p2.included_keys == (t2, c2)
    assert set(p1.included_keys).isdisjoint(p2.included_keys)


# --- one packet: CodeRabbit items, a failed check and Codex findings -------------------------------


def test_mixed_packet_keeps_review_items_and_codex_findings_and_cuts_the_check():
    codex = codex_items(CodexReview(H1, tuple(finding(i, "detail " * 400) for i in range(4))))  # ~2.8 KB each
    items = [cr_item(1), cr_item(2), *codex]
    check = failed_check(9, big_log())  # ~16 KB
    big = [failed_check(10 + i, big_log()) for i in range(3)]
    p = build_packet(head_sha=H1, seq=1, scanner=FakeScanner(), items=items, failed_checks=[check, *big])
    assert p.size <= PACKET_CAP_BYTES
    keys = p.included_keys
    assert [k[0] for k in keys[:6]] == ["coderabbit", "coderabbit", "codex", "codex", "codex", "codex"]
    assert all(not i.truncated for i in p.items[:6])  # review findings are never cut
    assert all(k[1].startswith(H1 + "/") for k in keys if k[0] in ("codex", "check"))
    assert len(keys) + len(p.omitted_keys) == 10 and set(keys).isdisjoint(p.omitted_keys)
    assert p.omitted_keys and all(k[0] == "check" for k in p.omitted_keys)
    assert p.items[-1].truncated and p.items[-1].key[0] == "check"  # the check crossing the cap is cut
    assert "[major] Finding 0" in p.text and "omitted by the 48 KB packet cap" in p.text


def test_mixed_packet_under_the_cap_holds_everything_in_order():
    codex = codex_items(CodexReview(H1, (finding(1), finding(2))))
    p = build_packet(head_sha=H1, seq=2, scanner=FakeScanner(), items=[cr_item(1), *codex], failed_checks=[failed_check()])
    assert [k[0] for k in p.included_keys] == ["coderabbit", "codex", "codex", "check"]
    assert not p.omitted_keys and p.size <= PACKET_CAP_BYTES
    assert "### codex codex_finding " + H1 + "/F1 toy/strings.py:11 (by codex)" in p.text


def test_codex_findings_are_secret_scanned_like_any_item():
    scanner = FakeScanner("ghp_")
    codex = codex_items(CodexReview(H1, (finding(1, "leaked ghp_abcdef0123456789"),)))
    with pytest.raises(SecretDetected) as e:
        build_packet(head_sha=H1, seq=1, scanner=scanner, items=[cr_item(1), *codex])
    assert e.value.item_key[0] == "codex" and "ghp_abcdef" not in str(e.value)
