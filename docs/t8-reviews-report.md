# T8 reviews: report

Branch `t8-reviews`, built from `gaffer-v0.1-design` at 9d377c1. Only T8 files were touched. `config.py` and `conftest.py` are unchanged.

## What was built

`src/gaffer/reviews/` is a package, so `from gaffer import reviews` still works:

| Module | Contents | Ledger |
|---|---|---|
| `junit.py` | JUnit adapters for pytest, Jest, Vitest, PHPUnit (per-file runs) and generic. They produce one uniform `Case`: identity, repo-relative file, outcome, exception type, an assertion flag, the exception summary, and a collection-error flag. It also holds `CriterionMap`, `match_inventory` and runner helpers (`missing_runner_flags`, `runner_env`, `phpunit_file_command`). | R4, R7, R22, R28, R29 |
| `redfirst.py` | `classify_red_first`. It classifies by exception type, matches `expected_missing` symbols by whole word (full name or last component), and accepts a file-level collection error that names a symbol as a valid red. It also implements already_passing, the coverage rule and `retry_feedback()`. | R5 as amended by R28, R22, R29 |
| `checks.py` | `evaluate_required_checks` (conclusion mapping, `skippable_checks`, `check_appear_timeout` leading to MISSING). `collect_check_evidence` fetches the title, summary, up to 50 annotations and, for GitHub Actions, the job log. `log_tail` strips ANSI, keeps the last 200 lines and caps at 16 KB from the end. | R11, R13, R27 |
| `coderabbit.py` | `coderabbit_status`: the exact `Review completed` signal; skipped, paused or never-started become unverified with a reason; unrecognized descriptions are logged verbatim. `admit` implements the admission rules. `parse_review_body` handles the outside-diff and nitpick sections, with outside-diff dedupe. It also holds `NORMALIZE_PATTERN`, `content_hash` and `pr_body_outside_release_notes`. | R12, R27 |
| `rounds.py` | `Round` covers completion, the 60 s settle that resets on new admitted items, CHECK_MISSING, TIMED_OUT, late items and `open_late_round` (seq+1). `RoundState` is persistable and counts `supervised_seconds` from monotonic deltas within one process, and it records sleep gaps. The `Clock` protocol is injectable. `poll_round`, `fetch_snapshot` and `evaluate_snapshot` hold the AI-free poll glue. | R8, R9, R17, R25, R27 |
| `packet.py` | `build_packet` orders content as admitted items, then failing tests, then failed checks. Caps are 16 KB per check and 48 KB per packet, both cut from the end. A `SecretScanner` hook runs before each item enters; a hit raises `SecretDetected`. Omitted keys are listed. | R12, R13, R15, R24, R27 |
| `github.py` | The `GitHubReader` protocol, plus `UrllibGitHubReader`, a stdlib client covering REST pagination, GraphQL `reviewThreads` pagination and raw job logs. Its token is an unredirected header, so it never follows the log redirect to blob storage. Every failure raises `GitHubError`. | R13, Experiment 2 §6 |

Tests (146 in total):
- `tests/test_junit.py`, `test_redfirst.py`, `test_required_checks.py`, `test_coderabbit_admission.py`, `test_rounds.py`, `test_packet.py`.
- Shared fakes live in `tests/reviews_fakes.py`: `FakeGitHub`, `FakeClock` (monotonic stops while the Mac "sleeps") and `FakeScanner`.
- `test_coderabbit_admission.py` has one test per directory in `tests/fixtures/coderabbit/`, each checked against that directory's `expected.json`. A guard test fails if a fixture directory is added without a test.
- No test uses the network. The urllib client is tested through a fake opener.

### JUnit fixture provenance (`tests/fixtures/junit/`)

| Dir | Source |
|---|---|
| `pytest/` | Rebuilt from spike proof D (`spike/evidence/d_py_*.json`, `e_verify_py.txt`). The testcase fields and texts are the recorded ones, inside a standard xunit2 envelope. `head_skip_xfail.xml` is synthetic and uses pytest's documented skip and xfail shapes. |
| `jest/` | Rebuilt from proof D (`d_js_*.json`), using jest-junit with the spike's env. `empty.xml` is the shape jest-junit writes when no test runs (the watchman case). |
| `vitest/` | **Unverified.** Written from Vitest's documented `junit` reporter; Vitest was never run. |
| `phpunit/` | Rebuilt from 3b step 6 (`p_phpunit.txt`). `missingbase.out` and `parse.out` are the stdout of the runs that wrote no JUnit file. |
| `generic/` | Synthetic, in go-junit-report style. |

## Test command and result

```
uv run pytest
319 passed in 3.92s   (T8's six files: 146 passed)
```

## Interfaces exposed (for T9)

- **Tests.**
  - `parse_junit(runner, junit_bytes|None, root=<verify worktree>, exists=<repo-relative path -> bool>) -> RunResult`, and `parse_phpunit_runs([PhpunitFileRun(test_file, junit_bytes|None, output)], root=…)`.
  - `match_inventory(cmap, run) -> Inventory`: `.all_passed` gates VERIFY and READY, and `.failing` feeds the packets.
  - `classify_red_first(cmap, base_run) -> RedFirstResult`, whose `.status` is `ok`, `tester_invalid`, `already_passing` or `run_failed`.
- **Rounds.**
  - Create a round with `Round.open(head, clock=SystemClock(), round_timeout=repo.round_timeout)`, or restore one with `Round(RoundState.from_dict(row), …)`.
  - Then loop on `poll_round(rnd, gh, repo=…, pr_number=…, operator_login=cfg.operator_login, processed_keys=…, codex_recorded=…)`.
  - Map the outcomes: COMPLETE produces a packet from `outcome.packet_keys`; CHECK_MISSING is BLOCKED: required_check_missing; TIMED_OUT is BLOCKED: review_timeout.
  - `GitHubError` and `IncompleteData` mean "poll again"; they never mean completion.
  - `snapshot.remote_head` and `pull.state` are exposed for the foreign_head and CLOSED checks.
  - Late items: call `rnd.late_keys(admitted, processed)`, then `rnd.open_late_round(late)`.
- **Packets.**
  - `build_packet(head_sha, seq, scanner, items, failing_tests, failed_checks)`.
  - Mark only `packet.included_keys` processed. `omitted_keys` stay unprocessed.
  - `SecretDetected` is BLOCKED: secret_detected; its findings are already redacted.
- **Report.** The report needs:
  - `RoundState.coderabbit_state` and `coderabbit_reason` (unverified and why);
  - `Admission.ignored`, `.context` and `.not_reviewed_files`;
  - `RequiredChecks.no_ci_evidence` (the NO CI EVIDENCE stamp);
  - `RunResult.notes` (the generic xfail statement);
  - `RoundState.sleep_gaps` and `Round.wall_elapsed()`.

## Interfaces needed from other tasks

- **T7 (workspace).**
  - A gitleaks object that satisfies `packet.SecretScanner`: `scan(text) -> Sequence[SecretFinding(rule, description)]`, which must never return the secret itself.
  - The test runner returns JUnit bytes, or `None` when there is no file, plus `root`, the verification worktree path. reviews never reads an exit code.
  - For PHPUnit, T7 runs one process per test file (`phpunit_file_command`) and returns a `PhpunitFileRun` for each. File discovery (for example `**/*Test.php` under `test_dirs`) is T7's or T9's call.
- **T6 (store).** Persist:
  - `RoundState.to_dict()`, which includes `supervised_seconds`;
  - processed item keys `(source, item_id, content_hash)`;
  - packets with their included and omitted keys.
- **T5 (children).** The tester output must carry:
  - `tests[{criterion_id, label: acceptance|guard, file, classname, name}]`;
  - `expected_missing`;
  - `criteria[{id}]`;
  - an `untestable` list of ids, which T5 should confirm. `CriterionMap.from_tester_output` reads this shape.
- **T9 (runner).**
  - Supply `codex_recorded`.
  - Build `UrllibGitHubReader(config.github_token())`.
  - Decide whether Codex review findings become packet items (see 10 below).

## Config changes suggested (config.py not edited)

1. **Validate runner flags in `gaffer.toml`** with `reviews.junit.missing_runner_flags(runner, test_command)`. pytest needs `--continue-on-collection-errors`. Jest needs `--no-watchman`. PHPUnit needs `--log-junit` and `--do-not-cache-result`. Today a misconfigured `test_command` is only caught when a run fails.
2. **Jest's jest-junit environment** (`runner_env`) has to be set by whoever runs the tests. It isn't in `gaffer.toml`.
3. **Vitest's reporter flags** (`--reporter=junit`, `--outputFile.junit=…`) are unverified and not enforced.

## Decisions that looked wrong, missing or ambiguous

I recorded these here and didn't change the ledger. The choices I made are in the code and tested.

1. **R5 coverage vs guard-only criteria (looks wrong).** R5 requires "at least one acceptance test per testable criterion". The spike's own tester output covers C3 ("continues to upper-case inner text") with a guard only, so under R5 as written it is `tester_invalid` (`test_guard_only_criterion_fails_coverage`). I enforced the rule as written. Mark should choose one of two fixes: count guard-only criteria as covered, or have the tester mark such criteria as "guard-only / already satisfied".
2. **Precedence when red-first finds both problems** (not specified). If one test is already passing and another is invalid, `already_passing` wins: it is terminal and suggests the issue is already fixed, so a retry would be wasted.
3. **Guards on base** (not specified). A guard may fail on base by assertion or by naming an `expected_missing` symbol (`guard_fails_on_base`, not blocking). A guard that fails with an unrelated exception (a typo, a broken import) or is skipped is invalid, because it couldn't pass at the end either.
4. **Meaning of "cut from the end" for caps.** A check keeps its newest log lines (R27). A packet keeps its first blocks, in the order admitted items, failing tests, failed checks. The 48 KB cap therefore cuts CI logs before review findings. The block that crosses the cap is truncated, and later ones are omitted, listed and left unprocessed.
5. **Per-failing-test output cap of 4 KB.** R24 says "bounded" but gives no number.
6. **When the secret scanner runs.** It scans each item as it would enter the packet, after the per-check cap and before the packet cap. Any hit blocks the whole packet (R15).
7. **Required checks also match commit-status contexts** when no check run has that name (for status-based CI). R11 only says "check names". Drop this if Mark wants check runs only.
8. **Appear timeouts are measured in supervised seconds since the push.** Both `check_appear_timeout` and `coderabbit_appear_timeout` follow R25; the ledger only says "after push". A seq+1 round carries the elapsed time forward.
9. **The settle also applies to unverified CodeRabbit.** I read "complete the round at once" (R27) as "don't wait out the 10 min or 30 min timeout"; the 60 s settle still runs.
10. **Codex review findings as packet items.** Design step 7 says packets include the Codex review, but the v0.1 plan's PACKET line doesn't. `build_packet` accepts any `FeedbackItem` (`Source.CODEX` exists). T9 or Mark should decide.
11. **CodeRabbit review bodies are admitted only for the current head** (`commit_id == head`). Otherwise the round-2 outside-diff duplicate would resurface as a new item once its thread resolves in round 3. The ledger doesn't say.
12. **The operator's inline comments and thread replies are admitted**, under the thread rules: resolved ones are ignored and outdated ones are context. R27 makes only CodeRabbit replies context; R12 says "comments by Mark".
13. **An inline comment with no GraphQL thread raises `IncompleteData`**, which is treated like an API error. The alternative would silently guess its resolved or outdated state.
14. **xfail for Jest and Vitest.** `test.failing` and `it.fails` show as passed in JUnit, so they can't be detected. The ledger only requires the "xfail detection unavailable" statement for generic. Today R7's trailer rule and the Codex review are the only guard.
15. **PHPUnit assertion type.** Only `ExpectationFailedException` counts, as R29 says. `$this->fail()` (`AssertionFailedError`) is therefore an invalid red unless it names a symbol. Adding it is a one-line change.
16. **The generic runner can't type its failures.** An untyped `<failure>` with no exception words (`…Error`, `…Exception`, `panic:`, `Traceback`) counts as an assertion. Typed failures use the same type list as the other adapters.
17. **Identity matching collapses whitespace in classname and name.** jest-junit writes `" shout uppercases"`, with a leading space, for tests outside a `describe`.
18. **CodeRabbit body formats seen only partly.** The October 2026 outside-diff layout is fixture-tested. The nitpick layout and the "Files not reviewed due to moderation or processing errors" section are tested only with synthetic bodies in the earlier layout, because no current-format sample exists.
19. **The GitHub client is minimal.** It doesn't handle rate limits, use ETags or fetch check suites. The "no coderabbitai check suite" note is an optional parameter, `check_suite_present`, which `poll_round` doesn't fill.
20. **Vitest is unverified end to end.** Its fixtures come from the docs. The first real Vitest repo should replace `tests/fixtures/junit/vitest/base.xml` with real output.

No new dependencies were needed: the package uses the stdlib, and only the tests use pytest.
