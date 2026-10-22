# T6 report: store

- **Branch:** `t6-store` (from `gaffer-v0.1-design`). Not pushed.
- **Code commit:** `012bf74`. This report is in the commit after it.
- **Files:** `src/gaffer/store.py`, `tests/test_store.py`. `config.py` is unchanged.
- **Verify:** `uv run pytest` passes all 258 tests (85 of them in `tests/test_store.py`), in about 11 s. `uv run pytest tests/test_store.py` runs just the store tests.
- **Dependencies:** none added. The store uses stdlib `sqlite3` (SQLite 3.53 on Python 3.14) and tests use pytest.
- **Scope:** T6 only. Nothing outward-facing. Tests write only under pytest's `tmp_path` and never touch `~/.gaffer`.

## What's there

**Storage.** The database is `~/.gaffer/state.db` (`default_db_path()`). The store creates `~/.gaffer` with mode 0700 and the file with mode 0600. Job clones are not stored here; they live under `job_root` (R30), and the job records `clone_path`. The connection runs in WAL mode with `synchronous=FULL`, foreign keys on and `STRICT` tables. On open the store runs `quick_check`, and a corrupt file raises `StoreCorrupt` (exit 4).

**Transactions.** Every write method is a single `BEGIN IMMEDIATE … COMMIT`. Multi-row writes are atomic: a test run with its testcases, a packet with its item assignment and cycle count, and a resume result with item processing.

**Schema versioning.** The schema version is `PRAGMA user_version`, and `MIGRATIONS[i]` takes the schema from version i to i+1. Each migration step runs in its own transaction, including the `user_version` bump, so a failed or killed migration leaves the previous version intact. A database from a newer gaffer raises `SchemaTooNew` (exit 4) and is left untouched.

**Tables and rules:**

| Table | Rule from the ledger |
|---|---|
| `jobs` | Issue key, team, repo, branch, clone path, base SHA, tester commit SHA, pushed head, PR number, state, BLOCKED reason/message/step, criterion map (JSON), pre-assigned writer session ID (R9), cycles used/granted (R8), `reconstructed` flag (R9), generation and `archived_at` (R8 `--fresh`). |
| (one-active constraint) | A generated column `active` is 1 for any non-terminal state, and a unique index on it allows **one active job at a time**. A second unique index allows **one live (unarchived) job per issue**. Both are enforced by the schema as well as the API (`ActiveJobExists`, exit 4, names the active job). CHECKs: BLOCKED ⇔ reason set; archived ⇒ terminal; `cycles_used ≤ 3 + cycles_granted`. |
| `cycle_grants` | Each `--more-cycles N` is recorded (R8). |
| `attempts` | One row per child launch: role, step, pre-assigned session ID written *before* launch (R9), outcome and CLI usage (design "Seed data for B"). |
| `leases` | Role, PID, process start time, process group and session ID. At most one unreleased lease per job and role. `alive_leases(job, probe)` applies R9 (alive = PID exists with the same start time) and releases dead leases as `dead`. The release reason is recorded (e.g. `sigterm`). |
| `test_runs` + `test_results` | R4: keyed (job, head SHA, kind, attempt). Kind is `base` (base failures kept separately), `red_first` or `verify`. Results are per test identity (file, classname, name; R22), with outcome passed/failed/error/skipped/xfailed/missing (R7) plus exception type and message (R28). |
| `reviews` | One Codex review per (job, head). A second `record_review` returns the recorded review and `False`, so a rerun reuses it. |
| `rounds` | Unique `(repo, PR, head, seq)`. `seq` is one past the last round on that head, so a rerun after review_timeout opens seq+1 (R8). A job has at most one open round. Each round stores a snapshot of `deadline_seconds`, plus `supervised_seconds` (R25), outcome, and CodeRabbit verified/unverified with a reason (R27). |
| `feedback_items` | Unique `(job, source, item id, normalized content hash)` (R12, R27). `add_item` is idempotent and returns `(item, created)`. An edited comment gets a new hash, so it becomes a new item. Ignored items are stored with `admitted=0` and their reason for the report. Processed state, disposition (fixed + SHA / declined + reason) and packet are recorded per item. |
| `packets` | Content and sha256 `packet_hash` are recorded before the resume. Creating a packet also assigns its items and counts one repair cycle (R8, R24), and raises `RepairLimitReached` when no cycles are left. A job has at most one unresolved packet. Re-creating the same packet returns it (the recovery table's "resume with the same packet"). `record_resume_result` stores the result and marks the items processed in one transaction. |
| `cli_evidence` | R2: Claude and Codex versions, auth status output and pass/fail, per job and per preflight. Anything that still contains an email address is refused. |
| `doctor_runs` | R18: pass/fail and results per (claude_version, codex_version). `doctor_needed(cv, xv)` is true when either version differs from the **last pass**. |
| `events` | Job log for the report: `sleep_gap` (per round), `adopt_head`, `state_lost`, `reconstructed`, and anything the runner logs, such as a writer group SIGTERM (R9). |

**Supervised time (R25, R17).** `store.supervise(round_id)` returns a `RoundSupervisor`. Each `tick()` adds the monotonic delta since the previous tick to the round's persisted `supervised_seconds`. The baseline is taken when `supervise()` is called, so time while gaffer is stopped, crashed or rebooting is never counted. On macOS `time.monotonic()` is `mach_absolute_time()` (checked on this machine), which stops while the Mac sleeps, so sleep isn't counted either. When the wall clock moves more than 120 s further than the monotonic clock, the tick records a `sleep_gap` event (R17's recording, kept by R25). `Round.wall_elapsed(now)` provides the wall-clock figure for the report. The baseline only advances after the credit commits, so a failed write is retried on the next tick rather than lost. `Clock` is a protocol, and tests use a fake clock with `advance`, `sleep` and `reboot`.

**state_lost (R9).** `check_state_lost(issue_key, remote_branches)` reports lost state when a `gaffer/<key>` or `gaffer/<key>-N` branch exists on GitHub but no job record (archived ones included) owns it. `record_state_lost` creates the job as BLOCKED: state_lost on the newest generation, with its PR number. `reconstruct_job` (for `--adopt-head`) takes the caller's inferred base (merge-base), tester commit (first commit) and head. It sets `reconstructed=1` so the report can say the job ran on inferred evidence, and it clears the writer session (fresh writer with handoff).

**Tests** (`tests/test_store.py`):
- **Crash and reopen for every kind of write** (31 cases, plus a guard test that fails if a new write method isn't in the table). For each case, a child Python process performs the write and is SIGKILLed twice over: once inside the transaction just before COMMIT, and once right after the write returns. After the first kill, the reopened database passes `integrity_check` and holds exactly the previous rows. After the second, it holds changed rows and reopens cleanly. A kill during the initial migration leaves `user_version` 0, and the next open migrates.
- **One active job at a time.** The API refuses a second active job, a rerun of a BLOCKED job while another is active, and a raw SQL insert that bypasses the API. Also covered: one live job per issue, `--fresh` generations (`gaffer/ENG-1-2`), and archiving only CLOSED jobs.
- **Idempotent item insert.** A repeat insert returns the existing item. An edited body is a new item. Sources are separate keys, and keys are per job.
- **Supervised time with a controllable clock.** Covers monotonic accumulation, an hour of sleep (not counted, gap recorded) versus a 2-minute skew (not recorded), crash, reboot and reopen (downtime not counted, persisted seconds kept), expiry exactly at the deadline, a monotonic clock moving backwards, and a failed tick being retried.
- **Other coverage:** packets, cycles and `--more-cycles`, resume results, review-once-per-head, per-head test runs being all-or-nothing, lease liveness with PID reuse, CLI evidence redaction, doctor version logic, state_lost detection and reconstruction, migrations forward, a failed migration rolling back, a newer schema being refused, and a corrupt file being refused.

## Interfaces

**Exposed for T9 (runner):** the `Store` class, plus `Job.ref` and `round_marker(job, round)`. `round_marker` returns `gaffer-round: <job.ref>/<head>/<seq>`. `Job.ref` is the issue key, or `<key>-<generation>` after `--fresh`, never the SQLite rowid (see item 6). Also exposed: `branch_name` and `branch_generation`; exceptions with `exit_code` (`ActiveJobExists` and `SchemaTooNew`/`StoreCorrupt` are 4, the rest 1); and `BASE_REPAIR_CYCLES`, `STEPS`, `BLOCKED_REASONS` and `Disposition`.

**Needed from other modules** (each defined as the smallest type in `store.py`):

| From | What | Where |
|---|---|---|
| T5 children | `ProcessProbe.start_time(pid) -> str \| None`, returning the start-time token in the same format children.py records in `acquire_lease(proc_start=…)` | `alive_leases()` |
| T7 workspace / T9 | The list of `RemoteBranch(branch, pr_number)` for the issue's `gaffer/<key>` and `gaffer/<key>-N` branches; merge-base and first commit for reconstruction | `check_state_lost()`, `reconstruct_job()` |
| T8 reviews | `CaseResult(file, classname, name, outcome, failure_type, message)` from the JUnit adapters. Each item's `content_hash`: sha256 hex of the body after R27 normalization. Item sources: `coderabbit`, `operator`, `codex`, `check`, `test` | `record_test_run()`, `add_item()` |
| T9 runner | A `Clock` (default `SystemClock`). The poll loop calls `RoundSupervisor.tick()` and acts on `Tick.expired` | rounds |

## Decisions that look wrong or are missing

1. **What "active" means after a crash.** Active = any non-terminal state. If gaffer crashes mid-job, that job stays active (`run.lock` is released, the DB row isn't). Every other issue is then refused with exit 4, naming the job, until that job is rerun to a terminal state. That matches "one job at a time", but the ledger never says whether `gaffer run ENG-2` should refuse in this case or park ENG-1 (for example as BLOCKED: stopped). That's T9's call; the store raises `ActiveJobExists` either way. Separately, the design's module table says "one active job per issue" and the brief says "one active job at a time". I enforce both: one live job per issue and one active job overall.
2. **The feedback item key is scoped to the job.** The ledger key is (source, item id, content hash). I added `job_id`, so a `--fresh` job never inherits processed items. More important for T8: failed checks and failing tests repeat across heads with identical text, so their `item_id` **must include the head SHA**. Otherwise an identical failure on a new head dedupes into an item that's already processed. Check-run IDs are already per head; test items need `"<head>:<test id>"`.
3. **Supervised time counts a suspended or hung gaffer.** Monotonic time on macOS excludes sleep but keeps running while a process is SIGSTOPped (Ctrl-Z) or stuck in a long call. R25 says time while gaffer is "stopped" isn't counted. If that includes Ctrl-Z, the ledger needs a rule, for example crediting at most about 2× the poll interval per tick. I didn't add a cap because R25 says the store counts monotonic deltas and nothing more.
4. **Reconstruction resets the cycle count.** After state_lost, `cycles_used` is unknown because R23 is still pending, so `reconstruct_job` defaults it to 0. A reconstructed job can therefore get up to 3 more repair cycles than it should. The method takes `cycles_used=` so T9 can pass a value once R23 decides where it comes from, such as the highest round marker.
5. **What `<job>` is in the round marker.** The ledger's `gaffer-round: <job>/<head>/<seq>` doesn't say what `<job>` is. If it's the SQLite id, a lost and recreated DB reuses ids and markers could collide. `round_marker()` uses `Job.ref` (the issue key plus the `--fresh` generation). The `gaffer-job: <id>` PR-body marker should use the same value. T9 and T7 need to agree.
6. **Doctor is keyed on the two CLI versions only (R18).** R28 made doctor probe sandbox-runtime as well (pinned 0.0.79) and Codex `web_search`. A change to the srt pin, or to gaffer's own probe suite, won't trigger a doctor rerun. Consider adding the srt version, and perhaps gaffer's version, to the key. That would be a one-column migration.
7. **`--more-cycles` grants aren't idempotent across a crash.** If gaffer crashes after recording a grant and the run is retried with the same flag, the job gets the cycles twice. Both grants are recorded and visible in the report. Making grants idempotent would need a per-invocation ID, which the ledger doesn't define.
8. **The BLOCKED reason list is fixed in code.** It's compiled from the design and ledger (25 codes) and checked in Python, so adding one needs no migration. `unmapped_team` is my own name: the design says "an unmapped team is BLOCKED" but gives no code (with T4's config validation this may be exit 4 instead).
9. **Missing dispositions.** If the writer omits an item, recording the resume result still marks it processed, with no disposition. Per the design, "items are marked processed when the resume result for that packet is recorded". READY's "every actionable item has a disposition" check has to look for processed admitted items whose disposition is NULL. T9's R10 gates may block before that point anyway.
10. **Lease liveness checks only the group leader.** `alive_leases` checks the PID and start time recorded in the lease. R9's "wait up to 2 min for the writer's process group" also needs a group check (`killpg(pgid, 0)`), which belongs in T5. The pgid stays recorded on released leases, so T1's `gaffer stop` can still find it.
11. **Red-first runs are keyed by the base SHA.** Red-first runs before the tester files are committed, so there is no new head yet. Retries are told apart by `attempt`. If T8 would rather key red-first runs by a hash of the tester files, that's one extra column.
12. **Config, for T4 (not edited):** `config.py` validates with `re.match` and patterns ending in `$`, and `$` also matches before a trailing newline. So `"ENG\n"` passes `_TEAM_KEY`, and the same holds for `_GITHUB_LOGIN`, `_GITHUB_REPO` and `_NAME`. I hit the same bug with the issue key and fixed it in the store with `fullmatch`. A one-line fix in config: use `fullmatch` or `\Z`.

No config change is needed for T6. `round_timeout` arrives as `deadline_seconds` when a round opens.
