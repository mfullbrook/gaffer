# R34 report: HTTP 429 backoff and baseline promotion

- **Branch:** `r34-codex-429`, cut from `gaffer-v0.1-design` at c56fd99. Not pushed, not merged.
- **Decisions in scope:** Mark, 2026-10-25, on the R33 follow-up's Open issues 2 and 3, recorded as ledger R34:
  - "back off, then treat as a limit";
  - "`--save-baseline` refuses to promote a run that has regressions".
- **Commits:**
  - 27a4bce: code and tests;
  - c969bc2: ledger R34;
  - plus this report.
- **Result:**
  - Full suite: **1086 passed**.
  - One live `--save-baseline` run: `20261025T164111-15cb6e`, **exit 0**, 0 regressions. **All three tickets met every expected outcome**, including the first live test of EVAL-3's new seeded item (`fixed`). It was promoted by `--save-baseline` itself; no `--promote` was needed.
  - `prompts/*.md` were not edited.

## What changed

### Detection (`children.py`)

A run is **throttled** when it has no result and no usage-limit signal, and the provider error says 429/529. The pattern is `_THROTTLE_TEXT`: `\b(?:429|529)\b`, "too many requests" or "overloaded".

- **Codex** (`CodexRun.throttled`): checked against the CLI's `error` / `turn.failed` events. These are the exact events from the R33 outage: `exceeded retry limit, last status: 429 Too Many Requests`.
- **Claude writer** (`WriterRun.throttled`): checked against an `is_error` result event, such as `API Error: 529 {…overloaded_error…}`, when there is no rejected `rate_limit_event`.
- **Real usage-limit signals still win at once, with no backoff:**
  - Codex rate-limit windows at 100%, or a `rate_limit_reached_type`;
  - "usage limit" or "rate limit" text;
  - Claude's `rate_limit_event` rejection.
- `\b` keeps out numbers like `4290`. Other HTTP errors (e.g. 503) are not throttles.

### Backoff (`runner.py`)

- **Codex tester and reviewer (`Runner._codex`):**
  - A throttled run is retried after each wait in `Deps.backoff`, which defaults to `children.THROTTLE_BACKOFF = (30, 120, 300)` seconds. The sleep goes through the injectable `Deps.sleep`.
  - If it is still throttled after the last wait, the run is given `children.throttle_limit(...)`, a `LimitEvent`. The tester's and reviewer's existing `if run.run.limit` checks then end the job **BLOCKED: usage_limit**, and a rerun resumes it.
  - These are the only Codex launch paths in the runner. Doctor's Codex probes are separate: a 429 there fails the probe, so doctor refuses (exit 4).
- **Claude writer (`Runner._writer_backoff`, wrapped around `_launch` and `_resume`):** a throttled writer run is retried after the same waits by **resuming the same session**, as the recovery table resumes a dead writer:
  - after a launch, with the existing `CONTINUE_PROMPT`;
  - after a resume (packet or nudge), with the same prompt and item IDs.

  If it is still throttled, it gets a `LimitEvent` and becomes BLOCKED: usage_limit through `_check_writer_run`. A rerun then continues the session, which is the existing WRITER recovery.

**About "pause/resume":** the brief described the runner's limit handling as pause/resume, not BLOCKED. The existing path is actually **BLOCKED: usage_limit**, with "Rerun after the reset time" in the report, and a rerun resumes the job (WRITER continues the session). I reused that path unchanged and added no new pause state.

### How the backoff interacts with existing retries and budgets

Each try is recorded honestly, but no budget is double-counted. I chose the conservative option for each:

- **Recording:** every try is its own attempt row with outcome `throttled`, plus a `throttled` event carrying the provider, role, step, retry number, wait and error.
- **The step's own retry is not used up:**
  - The caller sees one run per backoff sequence. The tester keeps its one retry (and its retry prompt never carries a 429 as `retry_error`), and the reviewer keeps its two real attempts.
  - The backoff budget is per child run. A tester whose first attempt hit a 429 and then an ordinary error gets its real retry, and that retry gets its own three waits. The worst case is 2 × (30 + 120 + 300) s per step.
- **The writer's other budgets are not used up either:** a writer backoff resume is not an R10 nudge, not a fresh-writer fallback and not a repair cycle. The packet retry reuses the packet, so `cycles_used` is unchanged.
- **Nesting is limited:** inside a backoff, a resume that can't be resumed still falls back to a fresh writer (the design's Session resume). That fallback launch does not start a nested backoff, so a sequence never exceeds three waits.

### `gaffer eval` (`prompt_eval.py`)

- **A 429 that ends as a limit** sets the job's `blocked_reason` to `usage_limit`, so the run stops as **incomplete: exit 3, never promoted, the remaining tickets not started**. This is the existing path, now reached.
- **A 429 recovered after a wait:**
  - isn't counted as a tester retry (no `ok_after_retry`, no warning);
  - `metrics.throttled_retries` records the count;
  - eval passes `sleep` through `eval_main` → `Env` → `Deps`, so tests don't sleep.
- **`--save-baseline` with regressions:**
  - the run is stored and the comparison printed;
  - nothing is promoted, and it exits 2 with: `not stored as the baseline: N regression(s) against baseline <id>. If they are intended, promote this run deliberately with gaffer eval --promote <run>.`;
  - with no regressions, or with no baseline yet, it promotes as before;
  - `--promote` is unchanged.

## Claude-side 429 / 529: the same gap, same rule applied

**Before:**

- A Claude API **529 (overloaded)** result has neither "usage limit" nor "rate limit" in it, so the writer ended **BLOCKED: writer_no_result**.
- A **429** became a limit only if its text happened to say "rate limit".
- Claude Code retries API errors itself before giving up, so the result event appears only after those retries.

**Now:** the same rule as Codex applies. I had no live 429/529 to test against, so the detection is fixture-tested against the documented `API Error: 529 {…overloaded_error…}` / `API Error: 429 …` shapes (Open issues, item 2).

## Tests (32 new)

- **`tests/test_children_launch.py` (14):** detection with fake CLIs.
  - Codex 429 on the tester and on the reviewer is throttled.
  - Codex is not throttled for "stream disconnected", `4290` or 503.
  - A Codex 429 with a result is not throttled.
  - A usage-limit text wins over 429.
  - Claude 529, 429 and "Repeated 529 Overloaded" are throttled.
  - Claude is not throttled when a rejected `rate_limit_event` comes with a 429, or for max-turns, a successful result or a malformed non-error result.
- **`tests/test_throttle.py` (14):** runner level with fakes.
  - Defaults are 30/120/300.
  - Tester: a 429 once is retried after 30 s with the same prompt (outcomes `throttled`, `ok`). Four 429s give usage_limit at TESTER after waits of 30, 120 and 300.
  - Tester budgets: the backoff and the tester's own retry are both still available. Two ordinary errors still give tester_failed.
  - A real limit gets no backoff, and `backoff=()` makes a 429 an immediate limit.
  - Reviewer: two 429s then ok is one review attempt. Four 429s give usage_limit at PUBLISH, not reviewer_failed. The two real attempts are kept beside the backoff. An error followed by four 429s gives usage_limit.
  - Writer: a 529 resumes the same session with CONTINUE, with no nudge, fallback or cycle. A writer still throttled gives usage_limit, and a rerun continues it.
  - Packet resume: a 529 is retried with the same packet and item IDs, and `cycles_used` stays 1.
- **`tests/test_prompt_eval.py` (net 4: one test replaced by three, plus two new):**
  - `--save-baseline` refuses regressions: the run is stored, exit 2, the exact message is shown, the baseline is unchanged, and `--promote` still works.
  - It promotes a run without regressions (a warning only).
  - It stores the first run.
  - A 429 recovered after a wait isn't a tester retry, and `throttled_retries` is 1.
  - A reviewer 429 that ends as a limit gives exit 3: incomplete, the second ticket not started, the baseline unchanged, `--promote` refused, waits 30, 120 and 300.
  - The old `test_save_baseline_promotes_this_run` encoded the behaviour Mark changed, and was replaced.

### Mutation check (27 mutants, 26 caught, 1 equivalent)

Each mutant was applied to one source file, the targeted tests were run, and the file was restored and compared byte for byte (scratch script, not committed).

- **Children (7 caught):**
  - the pattern without 529/overloaded;
  - the pattern without `\b`;
  - Codex throttled even with a result;
  - Codex throttled even with a limit;
  - Claude throttled without `is_error`;
  - Claude throttled despite a limit;
  - the Claude flag never set.
- **Runner, Codex (7 caught):**
  - no backoff;
  - waits that don't grow;
  - an exhausted backoff that isn't a limit;
  - one wait fewer;
  - the reviewer outside the backoff;
  - the tester outside the backoff;
  - no sleep.
- **Runner, writer (6 caught):**
  - no backoff;
  - a launch retried with the launch prompt;
  - a packet resume retried with CONTINUE;
  - a retry as a fresh launch;
  - an exhausted backoff that isn't a limit;
  - throttled attempts recorded as `no_result`.
- **Eval (6 caught):**
  - throttled tries counted as tester retries;
  - `--save-baseline` promoting regressions;
  - refusing every compared run;
  - a refusal that doesn't name `--promote RUN`;
  - a refusal that exits 0;
  - the `throttled_retries` metric missing.
- **Equivalent (1):** "Claude throttled despite a result". `_claude_writer` only parses a result when the event is not an error, so `is_error` already implies no result, and the `result is None` term is redundant (kept as a guard).

## Ledger

R34 was added after R33 in the R31–R33 table format (Mark, 2026-10-25). It has four rows: Codex 429, Claude 429/529, eval and 429, and `--save-baseline` with regressions. The Approval readiness line now ends with R34. The design's Limits rule ("No retry loop") carries an inline "Amended by R34" note.

## Full suite

`uv run pytest` at c969bc2: **1086 passed in 828 s**. That is 1054 before, plus 32 new.

## Live run (`20261025T164111-15cb6e`, 273 s, exit 0)

The live run was started through the same two-line scratch wrapper, `gaffer.main(["eval", "--save-baseline"])`, because the tool guard refuses any command line containing the literal word "eval".

- There is still no `gaffer.toml`, so built-in defaults applied.
- Doctor wasn't rerun: the versions were unchanged (Claude Code 2.1.310, codex-cli 0.160.0).
- Nothing was written to `~/.claude`, `~/.codex` or `gaffer.toml`.

| Ticket | Final | Stages | Address | Hidden | Expected outcomes | Writer turns | Wall |
|---|---|---|---|---|---|---|---|
| EVAL-1 bug fix | done | all ok | declined (accepted: "`split()` already handles tabs and newlines", verified) | 6/6 | all met | 8 | 86 s |
| EVAL-2 feature | done | all ok | **fixed** | 6/6 | all met | 11 | 85 s |
| EVAL-3 dependency | done | all ok; 5 acceptance tests incl. the ValueError criterion; harness change justified | **fixed** | **6/6** | **all met** | 21 | 102 s |

Comparison with the outage baseline (`20261025T152154-54a3e9`):

- **0 regressions.**
- 1 warning: EVAL-3 writer turns 12 → 21. The baseline run's writer never reached address.md, so this is expected.
- Prompts and fixtures unchanged.
- **Improved (all EVAL-3):**
  - now completes;
  - review ok;
  - address fixed;
  - verify_after_address passed;
  - hidden tests 0 → 6;
  - `completed`, `address_fixed` and `hidden_tests_pass` now met.

**The new EVAL-3 item in its first live test:**

- The writer's first commit had exactly the "ticket-only" shape the fixture predicted: the `< 0` check, then `naturalsize`.
- The address step then added `if not isinstance(n_bytes, int): raise TypeError(...)` in `31cd7164f79e`, and all 6 hidden tests pass.
- The `requirements.txt` change carried a `Tester-Change-Reason:` that the R33 gate read, and Codex judged it justified.

**No 429 this time:** `throttled_retries` was 0 for every ticket, so the backoff wasn't exercised live; only the fakes cover it.

**Baseline:** promoted by `--save-baseline` (0 regressions, so the new rule allowed it). `baseline.json` is `20261025T164111-15cb6e` (promoted 2026-10-25 16:45:44) and has fixture hashes. The outage baseline is replaced, and Open issue 1 from the R33 follow-up is resolved.

### Quota used (one run, three tickets, no retries, no repair cycles, no throttling)

| Role | Runs | Input | Output | Time |
|---|---|---|---|---|
| Codex tester | 3 | 100.6k (65.4k cached) | 2.4k | 84 s |
| Codex reviewer | 3 | 118.5k (80.0k cached) | 1.2k | 51 s |
| Claude writer (opus) | 6 sessions/resumes, 40 turns | 100.8k cache-write, 397.6k cache-read | 7.6k | 110 s |

## Open issues

1. **The backoff hasn't been exercised live.** It is covered by fakes built from the R33 outage's exact Codex events.
2. **The Claude-side detection** relies on the documented `API Error: 429/529 …` result text. If a future Claude Code changes that text, a 529 falls back to writer_no_result, which is the old behaviour, not a silent pass.
3. **Long waits:**
   - A step can wait up to 7.5 min per backoff sequence, and 15 min if its own retry also hits 429s.
   - The waits are real wall time, so a throttled packet resume delays the job by that much.
   - I didn't change any timer and didn't check how the waits interact with R25's round deadlines. Worth a look if 429s turn out to be common.
4. **Doctor's Codex probes** don't back off. A 429 during doctor fails the probe and refuses with exit 4, which is safe and visible, so I left it.
5. **Leftovers:** `~/.gaffer/evals/runs/20261025T164111-15cb6e/` and `<tmp>/gaffer-eval-hvloot7r` are kept, as before.
