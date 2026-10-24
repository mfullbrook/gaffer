# T11 report: `gaffer eval` (R19)

- **Branch:** `t11-eval`, cut from `gaffer-v0.1-design` at 924e1a3. Not pushed, not merged.
- **Decision in scope:** R19, answer A ("Three-ticket toy eval"), read with R27–R32. SC addition: "the `gaffer eval` baseline is stored before the first prompt change".
- **Commits:** cbbc036 (eval, fixtures, tests, runner seams), 393e56a (two fixes after the live baseline, below), plus this report.
- **Result:** the live baseline was stored (run `20261023T085217-670f72`) and a second live run compared against it (`20261023T090139-9e48b0`): **0 regressions, 0 warnings, exit 0**. The baseline is honest rather than green. EVAL-1 passes everything. EVAL-2 completes, but its writer declined the seeded item, and I think that decline was correct (my fixture's fault). EVAL-3 is **BLOCKED: tester_change_unexplained** in both runs, from a real gaffer bug that the eval found: Claude Code's `Co-Authored-By:` paragraph hides the writer's `Tester-Change-Reason:` trailer from the R7 gate. `prompts/*.md` were not edited.
- **Full suite:** see "Full suite".

## What was built

| File | What |
|---|---|
| `evals/toy/` (new) | The toy repo as plain files (no nested `.git`): a `toy` package (`textutil.py` with `shout` and a buggy `word_count`), one pytest file, `requirements.txt` (`pytest==9.1.1`), `pytest.ini`, `.gitignore`, `scripts/install.sh` (uv, cache under `$TMPDIR`), a README and a short `CLAUDE.md` that gaffer reads as untrusted repo guidance (R31). |
| `evals/tickets/EVAL-<n>/ticket.toml` (new) | One file per ticket standing in for a Linear issue: `key`, `kind`, `title`, `description` (with an "Acceptance criteria" checklist, as INTAKE requires), the one seeded `[feedback]` item, and `[expected]` outcomes. |
| `evals/tickets/EVAL-<n>/hidden_test.py` (new) | Hidden acceptance checks. No child ever sees them: gaffer writes them into a verification worktree of the final head after the writer is done and runs them with the toy's tests. |
| `src/gaffer/prompt_eval.py` (new) | Ticket loading and validation, toy materialisation, the eval config, `EvalRunner`, result collection, comparison, JSON storage and the `gaffer eval` command. |
| `src/gaffer/runner.py` | Two minimal seams plus the CLI (below). |
| `src/gaffer/__init__.py` | Docstring lists the subcommand and its exit codes. |
| `tests/test_prompt_eval.py` (new) | 69 tests (below). |

### How a run works

`gaffer eval [--config PATH] [--save-baseline | --promote RUN]`

1. Loads the tickets. Without `--config` and without `~/.gaffer/gaffer.toml` it runs under built-in defaults, as `gaffer doctor` does: writer model `opus`, Codex CLI default model, a stand-in vault dir and a temp job root. It never creates or edits `gaffer.toml`. With a config it uses that config's models, vault, allowed MCP and operator login; the config's repos are ignored.
2. Takes `~/.gaffer/run.lock` for the whole run (one gaffer at a time, R9).
3. Makes a fresh git repo of `evals/toy` (fixed author and date, so every run has the same base SHA) plus a local bare clone standing in for GitHub, both in a temp dir. The eval config is generated as TOML and loaded through `config.load_config`, so it gets the normal validation, harness paths and network allowlist (python: `pypi.org`, `files.pythonhosted.org`).
4. Drives each ticket through `EvalRunner`, a `runner.Runner` subclass, via the runner's own `_run_locked`:
   - PREFLIGHT (real `children.preflight`) and the **R18 doctor gate, shared with `gaffer run`**: if doctor is stale it is rerun, and a failure refuses with exit 4 naming the probes. Doctor results are read from and recorded in the real `~/.gaffer/state.db`, and doctor proves the user's config (or the defaults), never the eval config.
   - INTAKE reads the ticket file through a `LinearPort` (`TicketLinear`).
   - WORKSPACE, TESTER, RED_FIRST, WRITER with its gates, VERIFY with its local repair loop (R24): the production code paths and prompts, unchanged.
   - **PUBLISH is replaced:** a Codex review of the verified head (`review.md`, recorded as in production), then the ticket's seeded feedback item becomes a `local` packet (built by `reviews.packet.build_packet`, gitleaks-scanned, counted as a repair cycle) and RESUME sends it through `address.md` to the same writer session. Then VERIFY again, then the hidden tests, then READY.
   - There is no push, PR, round, CodeRabbit, Linear call or GitHub token. `Deps.github` raises `NoNetwork`, `Deps.token` returns None, and origin is the local bare repo.
5. Reads each ticket's outcome back from its own state.db (`collect`) and writes the run record.
6. With no baseline, a complete run becomes the baseline. Otherwise it prints the comparison and exits 2 on any regression. `--save-baseline` runs and then promotes; `--promote RUN` promotes a stored complete run without starting a child.

A usage limit (or an internal error) stops the run: the remaining tickets are not started, the run is recorded as incomplete (exit 3) and is never promoted.

### Seams in runner.py (`gaffer run` behaviour unchanged)

- `Deps.state_root: Path | None = None`: where per-job state dirs (child logs, `report.md`) live. None keeps `~/.gaffer/jobs`. Eval puts them under `~/.gaffer/evals/runs/<run>/jobs`, still inside the denied `~/.gaffer` (R28; `JobDirs.validate` enforces it).
- `doctor_gate(store, cfg, report, run, say)`: `Runner._doctor`'s body moved to a module function so eval can call it with the real store and the user's config. `Runner._doctor` calls it with exactly the old arguments.
- The `eval` subparser and its dispatch in `main()`.

Everything else is a subclass override in `prompt_eval.py` (`_publish`, `_doctor`). The full existing suite passes unchanged.

## The tickets and their expected outcomes

| Ticket | Kind | What it asks | Seeded feedback (CodeRabbit-style inline item) | Expected |
|---|---|---|---|---|
| EVAL-1 | bug fix | `word_count` splits on single spaces; fix repeated, leading/trailing spaces and the empty string; `"one two three"` still 3 | "tabs and newlines are whitespace too: `word_count("a\tb\nc")` must be 3" | red-first by **assertion**; seeded item `fixed` **or** `declined` (a `str.split()` fix already handles tabs, so declining as already handled is correct); 6 hidden tests pass |
| EVAL-2 | new feature, missing symbol | add `initials(name)` to `toy/textutil.py` | "`initials("jean-paul sartre")` should be `"JPS"`" | red-first by **missing symbol** (`initials`); seeded item `fixed`; 5 hidden tests pass |
| EVAL-3 | dependency add | new `toy/sizes.py` with `human_size()` returning `humanize.naturalsize()`; declare `humanize` in `requirements.txt` | "raise `ValueError` for negative sizes" | red-first by **missing symbol** (`toy.sizes`); seeded item `fixed`; final head's `requirements.txt` declares `humanize`; `humanize` installs from PyPI through `install_command` under sandbox-runtime (R14) in every verification worktree; 5 hidden tests pass |

Every ticket also expects the run to complete (READY). `requirements.txt` is a harness path (R7), so EVAL-3's manifest change needs a `Tester-Change-Reason:` trailer and gets a Codex harness judgement.

## What a baseline records

Per run: run id, start time and duration, whether it completed (and why not), the sha256 of each of `tester.md`, `implement.md`, `review.md` and `address.md`, the sha256 of the toy and of each ticket dir (since 393e56a), Claude and Codex CLI versions (from preflight), the srt pin, writer and Codex models, and the config used.

Per ticket:

- **final:** `done`, or `blocked: <reason> at <step>`, or `error`.
- **stages:** `tester`, `red_first`, `writer`, `verify`, `review`, `address`, `verify_after_address`, `hidden_tests`. Each is a success (`ok`, `valid`, `passed`, `fixed`, `declined`), a success after a retry or repair (`ok_after_retry`, `valid_after_retry`, `passed_after_repair`), or the BLOCKED reason / `not_reached`.
- **red-first:** whether it was valid, and each acceptance test's verdict in the accepted run.
- **tests:** acceptance, guard and untestable counts from the criterion map.
- **final tests green**, **review findings** count, **harness judgements** by verdict.
- **address:** the seeded item's disposition (and the decline reason).
- **hidden tests:** passed/total and which failed.
- **expected:** each expected outcome met or not.
- **metrics:** wall time, writer turns, local repair cycles, tester attempts, nudges, fresh-writer fallbacks, and per role (tester, writer, reviewer, test_run, install) attempts, seconds and summed token counts.

## Comparison

- **Regressions (exit 2):**
  - a stage that succeeded (first try or after a retry) now fails;
  - red-first no longer valid;
  - final tests no longer green;
  - fewer hidden tests passing;
  - an expected outcome no longer met;
  - a ticket that completed no longer completes, or isn't run at all.
- **Warnings (printed, exit 0):** a first-try stage now needing a retry or repair; more Codex findings; fewer acceptance tests from the tester ("tests weaker"); runs clearly longer (wall time over 1.5× **and** +120 s, or writer turns over 1.5× **and** +5).
- **Changes (printed):** prompt files changed since the baseline (named), eval fixtures changed since the baseline (named), CLI versions, models, a different correct disposition (e.g. `fixed` → `declined`), fewer findings.
- **Improvements (printed).**

## Storage choice: JSON files under `~/.gaffer/evals/`, not state.db

- `~/.gaffer/evals/baseline.json`: the promoted run, plus `promoted_at`.
- `~/.gaffer/evals/runs/<run>.json`: every run, kept.
- `~/.gaffer/evals/runs/<run>/`: that run's scratch: one `state.db` per ticket, the runner's log per ticket, the generated eval config, and per-job state dirs with `report.md` and child logs.
- The toy repo, its bare remote and the job clones go in a temp dir (outside `~/.gaffer`, which children can't read), kept after the run for inspection; the path is printed.

Why JSON rather than a state.db migration:

- A baseline is one document compared as a whole and promoted by copying; it has no relational queries and no concurrency beyond `run.lock`.
- It is readable and diffable by hand (`diff baseline.json runs/<run>.json`) and survives schema changes to state.db.
- Keeping eval jobs out of the real state.db means an eval job can never trip R31's "crashed active job" refusal for `gaffer run`, and the real jobs table holds only real work. Each ticket runs in its own scratch state.db with the production schema, so the runner code is unchanged.
- Everything sits under `~/.gaffer`, already denied to every child (R28). Files are written atomically with mode 0600.

Doctor runs are the exception: they are read from and recorded in the real state.db, so eval and `gaffer run` share one doctor history.

## Tests

`tests/test_prompt_eval.py`: 69 tests. Ticket runs use `runner_fakes.World` (real git, workspace, store and the runner state machine; fake children, gitleaks and clock). The eval's toy there is runner_fakes' app repo so the fake test runner's rules apply; a `FakeChildren` subclass also reports the hidden tests.

- **Baseline stored:** first run → exit 0, `baseline.json` equals the run, all eight stages, verdicts, hidden counts, expected outcomes, metrics, prompt hashes and CLI versions recorded; the seeded item reaches the resume prompt rendered from `address.md`; one tester, one Codex review, one launch and one resume per ticket; doctor ran once and recorded into the real store. Scratch lives under `~/.gaffer/evals/runs/<run>` (`state_root`), and the real store holds no eval job.
- **Unchanged run passes:** exit 0, "prompts changed since the baseline: none", "0 regression(s), 0 warning(s)", baseline not replaced, doctor not rerun.
- **Each regression kind** (end to end, exit 2 and the exact REGRESSION lines): declined seeded item (hidden test and expected outcome), verify failing to the cycle limit, tester invalid twice, reviewer failing twice, an acceptance test already passing on base, a forgotten dependency. Plus unit tests of `compare` for every flag, stage, hidden count, expected outcome, completed ticket and missing ticket.
- **Warnings and changes:** a tester retry is a warning (exit 0); a declined item alone is a change; findings, fewer tests, slower runs; small slowdowns are noise.
- **Prompt change reported:** an edited `implement.md` (via `children.PROMPTS_DIR`) is named and its hash differs.
- **Doctor:** a failing doctor refuses with exit 4 before any child and records no pass and no run; a version change reruns doctor and its report reaches the console; real `EvalChildren.doctor()` proves the user config or the defaults, never the eval config; defaults mode records the defaults deny list.
- **No network / GitHub / Linear:** with `urlopen`, `socket.connect`, `LinearClient`, `GitHubClient`, `push`, `open_pull_request`, `post_round_summary`, `github_token` and `linear_token` all made to fail, a full run passes; the fake GitHub and Linear see no request; the bare remote ends with only `refs/heads/main`.
- **Fixtures and expectations:** a changed ticket is named in the comparison ("eval fixtures changed since the baseline"), and a baseline without fixture hashes says "unknown". The red-first expectation table covers 7 cases, including EVAL-3's live mix of a missing-symbol red and an assertion red.
- **Other:** preflight failure and a held run lock refuse (4); a usage limit stops the run incomplete (3), unpromoted, remaining tickets not started; `--save-baseline` and `--promote` (and refusal of unknown or incomplete runs); CLI parsing and dispatch; ticket validation (6 bad files, no hidden tests).
- **The real fixtures:** the three tickets load with the right kinds and expectations; the toy materialises reproducibly and its own tests pass with real pytest; each hidden test file fails on the unchanged toy, and EVAL-1/EVAL-2's pass on a reference solution.

Before the live runs I also ran the toy once under the real sandboxes with no model (scratch script, not committed): install via sandbox-runtime from PyPI, the Codex-sandboxed pytest run, a red-first classification of a tester-style test, and EVAL-3's hidden tests on a hand-written solution with `humanize` installed. All worked.

### Mutation checks

Each mutant was applied to a copy of the tree, the targeted tests were run, and the file was restored and compared byte for byte. 36 mutants, all caught. The last three test 393e56a's additions (fixture change not detected; red-first requiring every red to be of the expected kind; invalid reds accepted). The others:

stage failure not a regression · degraded stage counted as a regression · final-tests-green/red-first flags not compared · hidden-count drop not flagged · expected-outcome flip not flagged · completed-ticket check removed · missing ticket not flagged · prompt-hash change not detected · prompt hashes read at import time · regression exits 0 · every run promoted · incomplete run promoted · incomplete run promotable with `--promote` · usage limit doesn't stop the run · doctor gate skipped in eval · doctor gate proves the eval config · real eval children's doctor proves the eval config · seeded feedback skipped · Codex review skipped · Codex findings added to the seeded packet · hidden tests always pass · hidden tests not run · dependency check always true · dependency name matches a prefix · dependency name case-sensitive · red-first first-try check ignored · tester retries not counted · GitHub port returns instead of raising · run lock not taken · `state_root` ignored · `doctor_gate` never refuses · ticket loader accepts a non-EVAL key · toy commit date not fixed.

The "dependency check always true" mutant survived the first pass: the fake toy had no `requirements.txt`, so the check returned early. The test now starts from a `requirements.txt` with a near miss (`humanize-extra`) and the writer adds `Humanize>=4`, which also pins the prefix and case rules. One planned mutant (an unused `_codex_reviews` call) was equivalent and was replaced by one that really adds findings to the packet.

### Full suite

`uv run pytest` at 393e56a: **1032 passed in 669 s** (963 before T11, plus 69 in `tests/test_prompt_eval.py`). No existing test changed. An earlier full run at cbbc036 gave 1021 passed. The suite ran alongside other work, so its duration isn't comparable with T10's 461 s.

## Live runs

Both ran with `uv run gaffer eval` (through a two-line wrapper that calls `gaffer.main(["eval"])`) on Mark's Mac, on 2026-10-23, with no gaffer.toml, so under built-in defaults:

- writer model `opus`, Codex CLI default model;
- Claude Code 2.1.308, codex-cli 0.160.0, sandbox-runtime 0.0.79.

Doctor wasn't rerun: T10's pass recorded under defaults still matched the versions and deny list. Nothing was written to `~/.gaffer/gaffer.toml`, `~/.claude` or `~/.codex`. Paths are redacted below; `<tmp>` is the system temp dir.

### Run 1: baseline (`20261023T085217-670f72`, 245 s, exit 0)

```
gaffer eval: no ~/.gaffer/gaffer.toml; running under built-in defaults (writer model opus, a stand-in vault entry, a temp job root).
gaffer eval: run 20261023T085217-670f72: 3 tickets; writer model opus; logs in ~/.gaffer/evals/runs/20261023T085217-670f72; job clones in <tmp>/gaffer-eval-5cs3wy9d/jobs.
  EVAL-1 (bug_fix): done
    tester ok, red_first valid, writer ok, verify passed, review ok, address declined, verify_after_address passed, hidden_tests passed
    red-first valid True; final tests green True; review findings 0; hidden 6/6; writer turns 9; 79 s
    expected: completed ok, red_first_assertion ok, address_fixed_or_declined ok, hidden_tests_pass ok
  EVAL-2 (feature_missing_symbol): done
    tester ok, red_first valid, writer ok, verify passed, review ok, address declined, verify_after_address passed, hidden_tests failed
    red-first valid True; final tests green True; review findings 0; hidden 4/5; writer turns 8; 91 s
    expected: completed ok, red_first_missing_symbol ok, address_fixed MISSED, hidden_tests_pass MISSED
  EVAL-3 (dependency_add): blocked: tester_change_unexplained at WRITER
    tester ok, red_first valid, writer tester_change_unexplained, verify not_reached, review not_reached, address not_reached, verify_after_address not_reached, hidden_tests not_reached
    red-first valid True; final tests green False; review findings None; hidden -/-; writer turns 12; 75 s
    expected: completed MISSED, red_first_missing_symbol MISSED, address_fixed MISSED, declares_humanize MISSED, hidden_tests_pass MISSED
gaffer eval: stored run 20261023T085217-670f72 as the baseline (~/.gaffer/evals/baseline.json).
```

### Run 2: compare (`20261023T090139-9e48b0`, 250 s, exit 0)

The stage outcomes were identical to run 1 for every ticket. Writer turns were 8 / 9 / 11 and wall times 89 / 77 / 85 s. EVAL-1's tester wrote 4 acceptance tests this time instead of 3.

```
gaffer eval: compared with baseline 20261023T085217-670f72 (stored 2026-10-23 08:56:22)
  prompts changed since the baseline: none
  eval fixtures changed since the baseline: unknown (the baseline has no fixture hashes)
  improved: EVAL-3: expected outcome red_first_missing_symbol now met
gaffer eval: 0 regression(s), 0 warning(s).
```

The one "improved" line comes from my code change between the runs (393e56a, below), not from the prompts. EVAL-3's red-first was the same in both runs.

### What the runs showed, per ticket

- **EVAL-1 (bug fix): everything as expected, both runs.**
  - The tester wrote 3 (then 4) acceptance tests that were valid reds by assertion, plus a guard.
  - The writer fixed the bug with `text.split()`.
  - The Codex review had 0 findings.
  - The writer **declined** the seeded tabs/newlines item, citing that `split()` already handles them and that it ran `word_count("a\tb\nc")`. That is the verify-before-fix behaviour address.md asks for.
  - Hidden tests: 6/6.
- **EVAL-2 (feature, missing symbol): completes. The writer declined the seeded item, and the hidden hyphen test fails (4/5).**
  - Red-first was valid via collection errors naming `initials`.
  - The decline reason: "the ticket says *Words are separated by whitespace*, so `jean-paul` is one word… that should be a separate ticket."
  - That is a reasonable answer to an out-of-scope item, so **the fixture is at fault**: my seeded feedback contradicts the ticket's own definition.
  - I left the fixture unchanged between the two runs so the comparison stayed like-for-like (Decisions, item 1).
- **EVAL-3 (dependency add): BLOCKED tester_change_unexplained at WRITER, both runs. A real gaffer bug.**
  - The writer did everything right:
    - it created `toy/sizes.py`;
    - it added `humanize` to `requirements.txt`;
    - it installed `humanize` 4.16.0 from PyPI inside its own sandbox with the toy's install script;
    - it ran the tests;
    - it put a `Tester-Change-Reason:` line in the commit.
  - Claude Code then appends its `Co-Authored-By:` attribution as a **separate final paragraph**. `workspace._trailer` reads `%(trailers:key=Tester-Change-Reason)`, and git takes trailers only from the last paragraph, so the gate saw no reason and blocked. Reproduce with `git log -1 --format='%(trailers)'` on the job clone's head: it prints only the `Co-Authored-By` line.
  - Any real job whose writer touches a tester, harness or CI file would block the same way. Open issues, item 1, has the options. I didn't fix it: it's outside T11 and it's an R7 security gate.
  - In run 1 the expected `red_first_missing_symbol` was marked MISSED. That was my bug: the tester validly used an assertion red for the "declared in requirements.txt" criterion, beside missing-symbol reds for `toy.sizes`.

### Changes made between the two live runs (393e56a)

The baseline exposed two problems in my code. I fixed both with fake tests and didn't rerun anything live for them:

1. **The red-first expectation now reads "at least one acceptance test red the expected way, and every red valid"**, instead of "every test red the expected way". The stored baseline keeps the old flag, which is why run 2 shows one "improved" line.
2. **Runs now record fixture hashes** (the toy and each ticket dir), and the comparison names changed fixtures next to changed prompts. The run-1 baseline predates this, so run 2 says "unknown". Fixture changes will be visible from the next promoted baseline on.

### Quota used

Six tickets in total, two runs of three, with no retries or repair cycles in either run.

| | Run 1 | Run 2 |
|---|---|---|
| Codex tester | 3 runs, 100.4k input (65.2k cached), 2.7k output, 87 s | 3 runs, 100.4k input (43.3k cached), 2.5k output, 89 s |
| Codex reviewer | 2 runs, 69.9k input (44.5k cached), 0.8k output, 40 s | 2 runs, 69.6k input (33.8k cached), 0.7k output, 33 s |
| Claude writer (opus) | 5 sessions/resumes, 80.4k cache-write, 296.4k cache-read, 5.5k output, 100 s, 29 turns | 5 sessions/resumes, 72.5k cache-write, 272.9k cache-read, 5.8k output, 107 s, 28 turns |
| Free (no model) | 12 installs, 9 sandboxed test runs, ~10 s | 12 installs, 9 sandboxed test runs, ~12 s |

There were also a no-model sandbox smoke run before the live runs and the existing doctor pass (not rerun). No usage limit was hit: both live runs completed before the session's later usage-limit pause. Exactly two live runs were made, and neither was re-run. I didn't read the Pro meter itself; the token counts are what the CLIs reported.

## Decisions for Mark

None of these is covered by the ledger. Where a choice was needed I took the conservative option.

1. **EVAL-2's seeded feedback contradicts its own ticket.** The writer's decline is arguably right, so the baseline records `address declined`, hidden 4/5 and `address_fixed` MISSED. I left it unchanged so the two live runs were like-for-like. Options:
   - (a) rewrite the feedback to be in scope, e.g. a real bug in the writer's code that a hidden test checks;
   - (b) change the ticket to define words so that hyphens split;
   - (c) accept `declined` (`address = ["fixed", "declined"]`).

   Any of these changes the fixture, and the next run will name it. Promote a fresh baseline after it.
2. **EVAL-3 is blocked by the trailer bug** (Open issues, item 1). Fixing it will change EVAL-3 from blocked to (probably) done: a large "improved" diff. Promote a new baseline afterwards with `gaffer eval --promote <run>`.
3. **Storage: JSON under `~/.gaffer/evals/`, not a state.db migration** (see "Storage choice"). Eval jobs run in per-ticket scratch state.dbs. Doctor history is shared with `gaffer run`.
4. **Doctor gate: "refuse if stale or failing" read as "same as `gaffer run`".** Stale → rerun doctor → refuse on failure, through the same `doctor_gate`. I didn't add a refuse-when-stale-without-rerunning mode.
5. **Defaults mode.** With no gaffer.toml the vault entry is a stand-in (as in `gaffer doctor`), so **your real vault isn't on the deny list during an eval**. The toy tickets carry no hostile text, but once gaffer.toml exists, eval uses its vault automatically, or you can pass `--config`.
6. **Only the seeded item is addressed.** Codex findings and harness judgements are recorded and compared (counts, warnings) but not put in the packet, although R31/R32 make them packet items in real runs. This keeps the address step comparable between runs. The alternative is to also pass Codex findings, which makes the address step depend on what Codex found.
7. **Hidden tests were added** as the independent judge of outcomes. R19 says "expected outcomes" and doesn't specify how they are checked. Children never see the hidden tests: they are written into a verification worktree after the writer has finished.
8. **What counts as a regression** (exit 2): a success turning into a failure, red-first validity lost, final tests no longer green, fewer hidden tests passing, an expected outcome lost, a ticket no longer completing or not run. **Warnings only:** retries and repairs, more findings, fewer acceptance tests, runs over 1.5× **and** +120 s or +5 writer turns. LLM runs are noisy (EVAL-1's test count went 3 → 4 between identical runs), so I didn't make speed or counts gate anything.
9. **Exit codes:** 0 pass or baseline stored, 1 error, 2 regression, 3 incomplete, 4 refused. A usage limit or an internal error stops the run, and incomplete runs are never promoted.
10. **`--promote RUN`** was added beside `--save-baseline`, so a run can be promoted without spending quota again.
11. **The seeded item is presented as a CodeRabbit inline comment** (`source = "coderabbit"`), the commonest real feedback. Each ticket file can set it to `operator` or `codex`.
12. **The toy carries a README and CLAUDE.md** that tell the writer how to install (uv with its cache under `$TMPDIR`). Without them, the writer's `uv` would try to write `~/.cache/uv`, which its sandbox denies. Open issues, item 2, has more.
13. **Leftovers are kept.** `~/.gaffer/evals/runs/<run>/` (logs, dbs, reports) and the temp dir with the toy and the job clones stay after a run for inspection, with no retention policy. The two live runs left 1.8 MB under `~/.gaffer/evals` and two `<tmp>/gaffer-eval-*` dirs of 84 MB each. Most of that is the `.venv` installed in each clone and verification worktree, and the OS clears temp eventually. A cleanup or retention rule is yours to choose.
14. **Every run executes all three tickets in order**, with no per-ticket filter, so baselines always compare whole runs.

## Open issues

1. **R7 trailer vs Claude Code attribution (found by this eval; blocks any writer change to a tester, harness or CI file).** `workspace._trailer` uses git's trailer parser, which reads only the last paragraph. Claude Code writes `Co-Authored-By:` as a paragraph of its own after the writer's `Tester-Change-Reason:`. Options:
   - (a) turn off Claude Code's commit attribution in the writer's generated settings, so the writer's trailer stays last (a children change, which doctor could probe);
   - (b) accept a `Tester-Change-Reason:` line in any paragraph of the message body;
   - (c) tell the writer in implement.md/address.md to put it in the final trailer block. That is a prompt change, so it needs this eval first (R19).

   (a) or (b) fixes it without touching prompts. It needs your decision, because it's the R7 gate.
2. **implement.md's Python install note** says "install with `uv`" but doesn't say that uv's cache must live inside the job dir. The toy's guidance covers this. A real repo without such guidance may see the writer's `uv` fail on `~/.cache/uv`. Possible fixes are a gaffer-set `UV_CACHE_DIR` in the writer env, or a prompt line, which needs the eval.
3. **The run-1 baseline has no fixture hashes** and was recorded under the old red-first expectation. Promoting a fresh run after items 1/2 (or Decisions 1/2) fixes both.
4. **The R10 gates give no nudge for a missing trailer**: it is an immediate BLOCKED. Real jobs will hit it (item 1), so it may be worth one nudge like the dirty-tree case.
5. **Not covered:** a Jest/PHP toy (R19 asks for one pytest toy only), the R31 plugin relaunch path and the fresh-writer fallback under eval. These run through the same runner code and are covered by T9's fakes.
