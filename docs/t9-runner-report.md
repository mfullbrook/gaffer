# T9 report: runner

- **Branch:** `t9-runner`, from `gaffer-v0.1-design` at 6a01a6e. Not pushed.
- **Files:**
  - `src/gaffer/runner.py`, which holds the state machine, locks, reconciliation, rerun flags, round comment, report and Linear client.
  - `src/gaffer/__init__.py`: the `gaffer` entry point now calls `runner.main`.
  - New tests: `tests/test_runner_states.py`, `tests/test_rerun_flags.py`, `tests/test_locks.py`.
  - `tests/runner_fakes.py`: the fake world (children, GitHub, Linear, clock, scanner) around disposable git repos.
- **Not modified:** `children.py`, `store.py`, `workspace.py`, `reviews/` and `config.py`. Every mismatch is handled by an adapter inside `runner.py` (see below).
- **R31 amendments branch: NOT merged.** The coordinator asked me to merge `r31-amendments` (6782c42) into `t9-runner`. Claude Code's permission classifier denied `git merge r31-amendments`, and even `git show r31-amendments:…`, as "modify shared resources", so I couldn't read the amendment report either. This branch is therefore built and tested against the pre-R31 modules. "Adapting to R31 modules" below lists exactly what to change once the merge is allowed.
- **Tests:** see "Test result".

## What was built

### `gaffer run <ISSUE> [--adopt-head] [--more-cycles N] [--continue] [--fresh]`
- **Exit codes (R8):** 0 READY, 1 internal error, 2 BLOCKED, 3 CLOSED, 4 refused.
- **Argument errors** exit 4, not argparse's 2, because 2 would read as BLOCKED.
- **Issue key (Arch #3):** argv only, and `fullmatch` against `[A-Z][A-Z0-9]*-\d+`, so `ENG-1\n` is refused.
- **Real dependencies** are built lazily in `main()`. A READY reprint never touches the Keychain, GitHub or Linear.

### Locks (R9, R31)
- **The two locks:** `flock` on `~/.gaffer/jobs/<KEY>.lock`, then on `~/.gaffer/run.lock`.
- **Holder stamp:** each lock file holds `<pid> <KEY>`.
- **When a lock is held:** the run exits 4, naming the holder PID and, for `run.lock`, the job holding it.
- **On exit:** the locks are released on any exit, a crash included.
- **Crashed active job (R31):** the DB still has an active job for another issue, but we hold `run.lock`, so nothing is driving it. The run exits 4 with: "job ENG-1 is still active (state WRITER) … Fix: finish it with `gaffer run ENG-1` (or, once it exists, `gaffer stop ENG-1`)". The job is never auto-parked.

### Liveness (R9)
On every rerun, the runner reaps leases before doing anything:
- **A dead leader whose process group is still alive** gets 2 minutes, then SIGTERM. The SIGTERM is recorded.
- **Any live child of the job, in any role,** means exit 4, naming the role, PID and group. No second writer starts.
- **Before ROUND,** any unreleased writer lease goes through the same group wait and SIGTERM (SC3).
- **children's `group_terminated`** is recorded and shown in the report.

### State machine
The steps are PREFLIGHT → INTAKE → WORKSPACE → TESTER → RED_FIRST → WRITER → (gates) → VERIFY → PUBLISH → ROUND → PACKET → RESUME → … → READY / BLOCKED / CLOSED. Every handler is re-entrant and checks real state before any side effect.

- **PREFLIGHT:** `children.preflight` runs on every invocation except a READY reprint. A failure exits 4 and creates no job. CLI evidence is recorded per job (R2).
- **INTAKE (D1):** a read-only Linear fetch. The issue is stored as an `intake` event. The deterministic acceptance-criteria check is a heading with content under it, or a checklist.
- **WORKSPACE:**
  - Creates the job clone with `create_workspace` in `job_root/<job>`, where `<job>` is the R31 marker.
  - A dir left behind by a failed create is removed first (T7 item 11).
  - Runs `install_command` once in the clone.
  - New job only: if a `gaffer/<KEY>[-N]` branch exists on the remote with no job record, the job is BLOCKED: state_lost.
- **TESTER and RED_FIRST:**
  - The tester gets one retry for errors, invalid output and an invalid red.
  - The files are written and committed, then the tests run in a verification worktree at that commit (R4) and are classified (R5, R22).
  - The tester commit is recorded in the store only after red-first passes. Otherwise the clone is reset to the base.
  - Recovery table row: "tester commit on branch? keep it : rerun tester".
- **WRITER:**
  - The session ID is pre-assigned and recorded before launch (R9).
  - If that session already spawned (from a `spawned` event), a rerun resumes it with `CONTINUE_PROMPT`.
  - The R10 gates run with exactly one `NUDGE_PROMPT` resume for a dirty tree. `done`/`blocked`, usage limit, malformed result and cited SHAs are all checked.
  - **R31 plugin relaunch:** an `InitCheckFailed` whose only problem is "plugins loaded" adds the plugin keys to `~/.gaffer/blocked-plugins.json` (persistent across jobs) and relaunches once with a fresh session ID. Any other init problem, or a second failure, is BLOCKED: isolation_failed.
  - A resume that hits the init check retries the same session once.
  - **Session-resume fallback (design):** if a resume produces no init event, a fresh session starts with a handoff (implement prompt, branch log and diffstat, current task). The report says so.
- **VERIFY (R4, R24):**
  - Runs the exact head in a fresh verification worktree. PHPUnit runs once per file (R29), and Jest gets `runner_env` as `export` prefixes.
  - `match_inventory` decides on tester tests (R22, R7).
  - Non-tester failures that also failed in the red-first base run don't block (R4). New ones count as regressions.
  - On a failure, a local packet of failing tests (gitleaks-scanned) is built and the same session resumes. That uses a repair cycle. A failing head is never published. With no cycles left: BLOCKED: verify_failed.
- **PUBLISH:**
  - Refuses to publish a head without a passing `verify` event.
  - Push (gitleaks, never force) with `expected_remote` set to the last pushed head. `PushRejected` is foreign_head.
  - Opens or reuses the PR (marker `gaffer-job: <job>`, ready for review).
  - One Codex review per head: one retry, then reviewer_failed. Findings are stored as `codex` items, which makes them packet items (R31).
  - Then waits out the writer group, then ROUND.
- **ROUND:**
  - AI-free polling every 60 s plus 0–10 s jitter (injectable sleep and RNG).
  - The store's `RoundSupervisor` persists supervised seconds (R25). The reviews `Round` decides completion.
  - Each poll checks the PR first: closed or merged is CLOSED (exit 3), a head gaffer didn't push is foreign_head, and `mergeable == false` or `dirty` is base_conflict.
  - GitHub and IncompleteData errors are logged and never count as a completed review. If they persist past the deadline, the result is review_timeout.
  - A crash between round completion and PACKET resumes at PACKET.
- **PACKET:** collects the admitted items (the round's packet keys plus Codex findings for the head) and the failed required checks. Each failed check gets its evidence and ANSI-stripped log tail (R13, R27) and becomes a `check` item.
  - **Nothing to do:** the round summary comment is posted, then READY is evaluated.
  - **Items to fix:** `build_packet` (secret scan, caps), then `create_packet`. That counts a cycle, and with no cycles left the job is BLOCKED: repair_limit.
- **RESUME:**
  - If the packet result is already recorded, the rerun continues past it. Otherwise it resumes the same session with the same packet (recovery table).
  - Gates run with the cited SHAs. Dispositions are recorded and the round summary comment is posted (idempotent by marker, D2/R9).
  - **If the head moved:** VERIFY, then PUBLISH, then ROUND on the new head.
  - **If every item was declined (head unchanged):** a new round seq+1 opens on the same head. It re-reads checks and catches late items (SC6), then READY is evaluated.
- **READY:** checks every design condition:
  - the last complete round is on the head;
  - required checks all passed, or NO CI EVIDENCE;
  - CodeRabbit is done;
  - a Codex review is recorded;
  - gaffer's verify passed on the head;
  - no admitted item is unpackaged or lacks a disposition;
  - the remote head equals the job head.

  Anything unmet is BLOCKED: not_ready with the list.

### Rerun flags (R8)
- **BLOCKED:** a rerun re-validates from the blocked step. Some reasons use a fixed step instead (`RERUN_STEP`): not_ready, review_timeout and required_check_missing go to ROUND (a new round), verify_failed goes to VERIFY, repair_limit to PACKET, and acceptance_criteria_missing to INTAKE.
- **`--more-cycles N`:** recorded with `grant_cycles`, and allowed on any rerun. Cycles never exceed 3 + the grants.
- **`--adopt-head`:** only for foreign_head and base_conflict.
  - Fetches the remote head and the base, and checks the branch out at that head.
  - The tester commit must still be an ancestor, else tester_commit_rewritten.
  - `base = merge-base`, recorded with `store.adopt_head`. Then VERIFY, PUBLISH (where the push is a no-op) and ROUND.
- **`--continue`:** only for READY. It opens a new round on the current head.
- **`--fresh`:** only for CLOSED.
  - Archives the old job and starts generation 2 on branch `gaffer/<KEY>-2`, marker `ENG-1.2` (R31). PR lookup is open PRs only.
  - A plain rerun of a CLOSED job prints the report and the `--fresh` hint, and exits 3.
- **READY:** a rerun reprints the stored `report.md` and exits 0, with no GitHub, Linear or child calls.

### Report
The report is `~/.gaffer/jobs/<job>/report.md`, built from the store only and printed at every terminal exit. It contains:
- The basics: state, BLOCKED reason with the message and wording below, the writer's question, PR URL, verified remote head, base, tester commit, cycles and grants, adopt-head events, NO CI EVIDENCE.
- **CodeRabbit (R27):** verified or **unverified**, with the reason, for the head and per round, plus supervised vs wall time and sleep gaps.
- **⚠ CI workflow changes (R30)**, placed before everything else, then the other tester and harness changes with their trailers (R7).
- **GitHub hosts open to sandboxed code** for PHP repos (R29).
- **Tests and items:** gaffer's test run as a criterion → test → result table, untestable criteria, tester files created or modified (R6), every feedback item's disposition, ignored comments with reasons (R12), and files CodeRabbit didn't review.
- **Codex:** the review summary and harness judgements.
- **Environment:** CLI versions, MCP servers outside the sandbox (R3), the writer model (R30) and the network allowlist.
- **R28 and R31 items:**
  - synced skills the writer saw (R28);
  - plugins blocked and relaunched (R31);
  - every `.git/config` restore (R31), or "never changed";
  - the fresh-writer fallback and writer-group SIGTERMs.
- Usage per attempt, and the not-covered gates.

### Round summary comment (D2, R9, R31)
- **When:** once per completed round, after its packet result is recorded, or when the round had nothing to address.
- **Contents:** CodeRabbit verified or unverified, the required checks, the Codex finding count and the dispositions.
- **Marker:** `gaffer-round: <job>/<head>/<seq>`, where `<job>` is the issue key plus the `--fresh` generation (`ENG-1`, `ENG-1.2`).
- **Idempotent:** `workspace.post_round_summary` skips posting if gaffer's own login already posted that marker.

### Linear (D1)
- **Client:** `LinearClient` sends a single `issue(id:)` GraphQL query over urllib. It never sends a mutation. The token is an unredirected `Authorization` header and is scrubbed from errors.
- **Token:** `runner.linear_token()` reads the Keychain (service `gaffer`, account `linear-token`). config.py has only `github_token()`, so the helper lives in runner.py. It should move to config.py.
- **Tests:** they use a fake Linear port and a fake urllib opener.

## Test result

```
uv run pytest
797 passed in 340.50s   (the 692 before T9, plus T9's 105)
```

This is on the pre-R31 base (6a01a6e); r31-amendments isn't merged (see the top).

T9's files have 105 tests: `test_runner_states.py` 75, `test_rerun_flags.py` 15, `test_locks.py` 15. Every run uses the fake world: disposable source and bare repos, real git, real `workspace` and `store` code, and fake children, GitHub, Linear and gitleaks. There are no model, GitHub or Linear calls. The round loop runs on a fake clock, so tests never sleep.

What's covered:
- the whole flow;
- 26 BLOCKED cases, parametrized;
- every recovery-table row (SC4, 8 cases), each checking no second PR, writer or comment;
- the R24 local loop;
- rounds, packets and late comments (SC6);
- Codex findings and failed checks as items;
- CodeRabbit unverified;
- the R28–R31 report items;
- each rerun flag;
- locks with a real second process holding the flock;
- a live writer as a real `sleep` process with its real start time;
- the Linear client.

## Adapters written around other modules (and the interface changes they imply)

| Where | Adapter in runner.py | Implied change |
|---|---|---|
| store.BLOCKED_REASONS | runner widens the frozenset at import with `isolation_failed`, `install_failed` and workspace's `writer_branch_moved`, `writer_history_rewritten`, `workspace_tampered`, `secret_scan_failed`, `foreign_pr` | store should list these codes (R31 may already add isolation_failed) |
| store.Job.ref | `job_marker()` gives R31's `ENG-12.2`; store's `ref` and `round_marker` give `ENG-12-2` | store.Job.ref should use the R31 dotted form (likely done on r31-amendments) |
| store rounds | reviews' `RoundState` (seen keys, settle anchors, CodeRabbit state, since-push) is kept as `round_state` events; the store row owns supervised_seconds | add a `state` JSON column to `rounds` |
| store jobs | issue snapshot (`intake` event) and `Workspace.to_record()` (`workspace` events) live in the job log | add `issue` and `workspace` JSON columns to `jobs` |
| store leases | role `test` (children) → `test_run` (store) | align role names |
| store sources | reviews `ci`/`tests` → store `check`/`test` | done on r31-amendments, per the coordinator |
| reviews CriterionMap | built from the stored tester doc, because `from_tester_output` read `untestable[].id` while children's schema writes `criterion_id` | fixed on r31-amendments |
| reviews packet scanner | `PacketScanner` maps workspace `RawFinding(rule_id, …)` to packet `SecretFinding(rule, …)` | one shared SecretScanner protocol |
| reviews rounds | the runner calls `fetch_snapshot` / `evaluate_snapshot` / `Round.observe` itself (not `poll_round`) to read the PR's state for CLOSED, foreign_head and base_conflict | `poll_round` could return the snapshot |
| workspace install | `InstallRunner` → `children.run_install_command`; `InstallRequest.env` (TMPDIR, Composer vars, `npm_config_cache`) can't be passed | children should take the extra env, or workspace should stop building it |
| workspace private helpers | `_auth_env` (ls-remote and the --adopt-head fetch), `_out`, `_status_paths`, `_resolve_commit`, `_is_ancestor` | expose `remote_branches()`, `fetch_and_adopt()`, `reset_to_base()` in workspace |
| children plugin block list | persisted by the runner in `~/.gaffer/blocked-plugins.json` | on r31-amendments: `record_blocked_plugins(paths.blocked_plugins, keys)` |
| config | `linear_token()` | move to config.py next to `github_token()` |

## Adapting to the R31 modules (to do after the merge is allowed)

These follow the coordinator's message. I couldn't inspect r31-amendments, so they are unverified:
1. **Doctor.** `store.doctor_needed`/`record_doctor_run` now require `srt_version`. The runner calls neither today, because doctor is T10. When T10 adds them, pass `children.SRT_VERSION`.
2. **Item keys.** Replace the runner's hand-built keys (`f"{head}:{test identity}"`, `f"{head}:{check_run_id}"`, `f"{head}:{finding.id}"`) with `failing_test_key`, `failed_check_key` and `codex_items`. Drop the `_RV_TO_STORE_SOURCE` mapping, because reviews now says `check`/`test`.
3. **Codex findings.** Pass `codex_reviews=[CodexReview(head, findings)]` to `admit`/`evaluate_snapshot`. Then **remove** the runner's own `codex` item creation in `_codex_review` and the codex branch in `_packet`, or each finding would be admitted twice. Keep passing `codex_recorded`.
4. **Repo guidance.** Call `read_repo_guidance(ws.clone, scanner)` once after the tester commit, store it, pass `guidance=` to `implement_prompt` and every `address_prompt` (and the handoff), and add `guidance.note` to the report.
5. **Plugin relaunch.** Use `e.plugins_only`, `e.plugin_keys` and `record_blocked_plugins(paths.blocked_plugins, …)` in `_init_failed`, and delete the runner's own `blocked-plugins.json` code. `RealChildren` already holds `paths`.
6. **Preserves.** `TesterResult.preserves` is new. `_criterion_doc` and `_tester_result` must carry it (the latter constructs `TesterResult` directly), and `runner_fakes.TESTER_OUTPUT` may need the new field.
7. **CriterionMap.** Build it with `CriterionMap.from_tester_output` instead of `_criterion_map`, now that it reads `criterion_id`.
8. **Markers.** If `store.Job.ref` is now `ENG-12.2`, `job_marker()` can return `job.ref`.

## BLOCKED reasons and report wording

Each line is the reason, the step a rerun re-validates from, and the report's "What to do" text (`runner.BLOCKED_HELP`).

| Reason | Rerun from | Wording |
|---|---|---|
| acceptance_criteria_missing | INTAKE | The Linear issue has no "Acceptance criteria" heading or checklist. Add one to the issue and rerun. |
| tester_failed | TESTER | The Codex tester errored, timed out or returned nothing twice. Check the tester log and rerun. |
| tester_invalid | TESTER / RED_FIRST | The tester's tests were invalid twice (bad paths, wrong red-first reason, missing coverage). Rerun to try a fresh tester. |
| already_passing | RED_FIRST | Acceptance tests already pass on the base: the issue may already be fixed. Check the listed tests. |
| no_testable_criteria | TESTER | The tester judged every criterion untestable. Make the criteria testable and rerun. |
| writer_no_result | WRITER / RESUME | The writer returned no valid structured result (or cited a commit that isn't new). Rerun to resume it. |
| writer_no_change | WRITER / RESUME | The writer made no commit (or marked items fixed without one). Rerun to resume it. |
| writer_dirty | WRITER / RESUME | The writer left uncommitted changes after one nudge. Rerun to resume it. |
| writer_blocked | WRITER / RESUME | The writer stopped with a question (printed below). Answer it in the issue and rerun. |
| tester_commit_rewritten | WRITER / RESUME | The tester commit is no longer an ancestor of the head. Restore it and rerun. |
| tester_change_unexplained | WRITER / RESUME | A writer commit changed a tester, harness or CI file without a Tester-Change-Reason: trailer. Rerun to resume the writer. |
| forbidden_path | WRITER / RESUME | A writer commit touched .claude/, .codex/ or .mcp.json. Remove the commit and rerun. |
| secret_detected | the step it was found in | gitleaks found a secret (redacted below) in a push, comment or packet. Remove it and rerun. |
| verify_failed | VERIFY | gaffer's own test run kept failing and the repair cycles ran out; nothing was published. Rerun with --more-cycles N. |
| repair_limit | PACKET | Review feedback remains and the repair cycles ran out. Rerun with --more-cycles N. |
| not_ready | ROUND | The round completed with nothing to resume but READY's conditions (listed below) aren't met. Fix them and rerun. |
| review_timeout | ROUND (new round) | The round didn't complete within round_timeout supervised seconds. Rerun to open a new round on the current head. |
| reviewer_failed | PUBLISH | The Codex reviewer failed twice for this head. Rerun to retry it. |
| required_check_missing | ROUND | A required check never appeared on the head within check_appear_timeout. Fix the workflow or gaffer.toml and rerun. |
| foreign_head | ROUND / PUBLISH | The PR head is a commit gaffer didn't push. Rerun with --adopt-head to accept it. |
| base_conflict | ROUND | The PR conflicts with the base. Merge or rebase it yourself, push, and rerun with --adopt-head. |
| usage_limit | the step it hit | A child hit a subscription usage limit. Rerun after the reset time. |
| state_lost | (none) | gaffer has no record of this issue's branch or PR (state.db lost). Rebuilding with --adopt-head isn't built yet (R9/R23, build step 5). |
| isolation_failed | WRITER / RESUME | A child's init event still showed a plugin, an unlisted MCP server or a non-subscription key after one relaunch (R28, R31). Check ~/.gaffer and rerun. |
| install_failed | WORKSPACE / VERIFY | install_command failed. Check the install log and rerun. |
| writer_branch_moved | WRITER / RESUME | The writer left HEAD detached or on another branch. Check out the job branch in the job clone and rerun. |
| writer_history_rewritten | WRITER / RESUME | The writer rewrote history gaffer already saw; gaffer never force-pushes. Restore the branch and rerun. |
| workspace_tampered | any | The job clone's .git was replaced. Remove the job dir and rerun with --fresh after closing the PR. |
| secret_scan_failed | the step it hit | gitleaks failed to run, so nothing was published (fail closed). Fix gitleaks and rerun. |
| foreign_pr | PUBLISH | An open PR for the branch doesn't carry this job's gaffer-job marker. Close it and rerun. |
| stopped, unmapped_team | — | In store's list; the runner never sets them (`gaffer stop` is T1; an unmapped team is refused, see below). |

## Decisions where I departed from or filled in the ledger

1. **Unmapped team is refused (exit 4), not BLOCKED.** The design says BLOCKED, but a job record needs a repo, and the fix is in gaffer.toml like any other config error. No job is created.
2. **A CLOSED rerun prints the report and exits 3,** not 4. R8 lists exit 4 for preflight or lock refusals, and nothing ran.
3. **`--fresh` on a non-CLOSED job, `--continue` on a non-READY job, and `--adopt-head` outside foreign_head/base_conflict all exit 4.**
4. **`report.md` lives in `~/.gaffer/jobs/<job>/`** (gaffer-private, denied to children) rather than the child-writable job dir under `job_root`. That way a READY reprint can't show text a child wrote.
5. **All items declined:** the round opens seq+1 on the same head before READY is evaluated, so late items and fresh check results are seen (SC6). R8 says "READY is evaluated". This costs one settle (60 s).
6. **required_check_missing closes the round as `timed_out`**, because the store has no `missing` outcome. foreign_head and base_conflict close it as `superseded`.
7. **Red-first runs on a committed candidate** in a verification worktree. The commit is recorded as the tester commit only after the red-first pass; otherwise the clone is reset to the base.
8. **VERIFY regressions:** a non-tester test failing on the head blocks READY unless it also failed in the red-first base run (R4's "base failures recorded separately").
9. **Codex harness judgements** of `unjustified` are listed in the report, not turned into packet items. R7 only says they are judged and listed.
10. **Every live child refuses the rerun, not just the writer** (R9: "liveness checks for every child role").
11. **The resume after an init-check kill keeps its session,** since the session already exists. Only a fresh launch gets a new ID (T5's note).
12. **Linear failures** (HTTP, not found, GraphQL errors) are exit 1. The job stays at INTAKE and a rerun refetches.
13. **The writer_blocked rerun resumes the same session.** There's no channel for Mark's answer in v0.1 (D1 defers Linear write-back), so the writer sees the issue text only.

## Open issues

- **The R31 merge was denied by the permission system.** See the top and "Adapting to the R31 modules". Until it is merged and adapted, this branch duplicates R31 behaviour that now lives in the modules: Codex items, the plugin block list and the dotted marker.
- **Not built (not T9, or explicitly later):**
  - doctor (R18/T10): preflight doesn't auto-run it on a version change;
  - `--adopt-head` reconstruction after state_lost (R9; build step 5, R23 unresolved). It is refused with an explanation.
  - `gaffer stop` (T1).
- **Repo CLAUDE.md as untrusted guidance (R31)** isn't wired: it needs r31-amendments' `read_repo_guidance`/`guidance=`.
- **GitHub rate-limit headers aren't honored** (the design asks for it); the reviews client doesn't expose them.
- **A second `--more-cycles` after a crash double-grants** (T6 item 7), and that is visible in the report.
- **InstallRequest env is dropped** by the install adapter (above). For Node this loses `npm_config_cache`.
- **The fake test runner** writes pytest-shaped JUnit from rules rather than running pytest. Real red-first behaviour is covered by T8's fixture tests and the spike.
- **`_tester_result` constructs `children.TesterResult` directly,** so a new required field (R31's `preserves`) breaks it until it's adapted.
