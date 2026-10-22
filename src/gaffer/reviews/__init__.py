"""reviews (T8): JUnit adapters, red-first, required checks, CodeRabbit
admission, rounds and packets. See each submodule's docstring for the ledger
records it implements."""

from .checks import (
    CheckEvidence,
    CheckState,
    RequiredChecks,
    collect_check_evidence,
    evaluate_required_checks,
    log_tail,
)
from .coderabbit import (
    Admission,
    CodeRabbitState,
    CodeRabbitStatus,
    CodexReview,
    FeedbackItem,
    IncompleteData,
    Source,
    admit,
    codex_item_id,
    codex_items,
    coderabbit_status,
    content_hash,
    normalize_body,
    pr_body_outside_release_notes,
)
from .github import GitHubError, GitHubReader, UrllibGitHubReader
from .junit import (
    Case,
    CriterionMap,
    ExpectedTest,
    Inventory,
    Outcome,
    PhpunitFileRun,
    RunResult,
    match_inventory,
    parse_junit,
    parse_phpunit_runs,
    missing_runner_flags,
)
from .packet import (
    Packet,
    SecretDetected,
    SecretFinding,
    SecretScanner,
    build_packet,
    failed_check_key,
    failing_test_key,
)
from .redfirst import RedFirstResult, RedFirstStatus, Verdict, classify_red_first
from .rounds import Clock, Round, RoundOutcome, RoundState, RoundStatus, SystemClock, poll_round

__all__ = [
    "Admission", "Case", "CheckEvidence", "CheckState", "Clock", "CodeRabbitState", "CodeRabbitStatus", "CodexReview",
    "CriterionMap", "ExpectedTest", "FeedbackItem", "GitHubError", "GitHubReader", "IncompleteData",
    "Inventory", "Outcome", "Packet", "PhpunitFileRun", "RedFirstResult", "RedFirstStatus",
    "RequiredChecks", "Round", "RoundOutcome", "RoundState", "RoundStatus", "RunResult",
    "SecretDetected", "SecretFinding", "SecretScanner", "Source", "SystemClock", "UrllibGitHubReader",
    "Verdict", "admit", "build_packet", "classify_red_first", "codex_item_id", "codex_items", "coderabbit_status",
    "failed_check_key", "failing_test_key",
    "collect_check_evidence", "content_hash", "evaluate_required_checks", "log_tail",
    "match_inventory", "normalize_body", "parse_junit", "parse_phpunit_runs", "poll_round",
    "pr_body_outside_release_notes", "missing_runner_flags",
]
