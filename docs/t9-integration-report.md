# T9 integration report: runner on the R31 modules, plus R32

- **Branch:** `t9-integration`, from `integrate-t9` at c2caffb (T4–T8 merged, R31 amendments, T9 runner, R32 recorded). Not pushed.
- **Scope:** the R31 follow-ups listed in `docs/t9-runner-report.md` ("Adapting to the R31 modules") and `docs/r31-amendments-report.md` ("Interface changes T9 must know"), and the three R32 rows. Every change was made in the module that owns it, with its tests.
- **Tests:** `uv run pytest` gives **863 passed, 0 failed, 0 skipped** in about 6 min 18 s. See "Test result".
- **No real calls.** Children, GitHub, Linear and the clock are fakes. The tests use temporary homes and nothing touches `~/.gaffer`, `~/.claude` or `~/.codex`. There are no new dependencies.

## A. T9 follow-up (runner.py and its tests)

### 1. Item keys and sources
- **VERIFY:** the local packet records each failing test under `reviews.failing_test_key(head, entry)`.
- **PACKET:** each failed check is recorded under `reviews.failed_check_key(head, evidence)`.
- **Store keys:** each store row's `(source, item_id, content_hash)` is the exact key `build_packet` puts in the packet. Packet keys map straight to store item ids, with no rebuilding.
- **Removed:** `_RV_TO_STORE_SOURCE`, the special case for `"ci"` keys and the runner's `_sha256` hashing. The reviews `Source` values (`check`/`test`) go straight into `Store.add_item`.
- Codex keys come from `codex_items` through admission (item 2).

### 2. Codex findings, counted once
- **Admission:** `_round` passes `codex_reviews=[CodexReview(head, findings, harness_judgements)]` to `evaluate_snapshot`. The runner calls `evaluate_snapshot` rather than `poll_round`; see the adapter table.
- **`codex_recorded`** is still passed as its own value, derived from the same lookup.
- **Removed:**
  - the item creation in `_codex_review`, which now only records the review;
  - the `source == "codex" and head == round head` branch in `_packet`.
- **Packet contents:** the packet holds exactly the round's admitted keys, whatever the source.
- **New test:** `test_each_codex_finding_is_in_the_packet_exactly_once`. It checks each of these:
  - two findings give two `codex` store rows;
  - two `codex` keys in the stored packet;
  - each title appears once in the packet text and once in the resume prompt;
  - two `item_ids`.

### 3. Repo guidance, read once
- **Read and recorded:** `Runner._read_guidance` calls `children.read_repo_guidance(clone, scanner)` and records the result as a `repo_guidance` event (`RepoGuidance.to_dict()`).
- **When:** right after `set_tester_commit`, before the state moves to WRITER. `_writer` calls it again before the first launch. That call is a no-op if the read is already recorded, and it covers a crash between the two steps. No writer has run at that point, so the clone is still at the tester commit.
- **Reuse:** `implement_prompt`, every `address_prompt` and the fresh-session handoff use `Runner._guidance(job)`, which is the recorded copy. It is never re-read, so a writer that edits CLAUDE.md or AGENTS.md changes nothing gaffer sends.
- **Report:** a new line, "Repo guidance (R31, R32): …", built from the notes.
- **Tests:**
  - guidance is read before the writer spawns;
  - the same text reaches both the launch and the address prompt, after the writer rewrote CLAUDE.md;
  - one `repo_guidance` event after a writer crash and rerun;
  - a scanner error leaves the job running (see item 9).

### 4. Plugin relaunch
- **Policy:** `_init_failed` uses `e.plugins_only` and `ch.record_blocked_plugins(Paths.for_home(home).blocked_plugins, e.plugin_keys)`. That is one relaunch with a fresh session ID for a launch, or the same session for a resume.
- **BLOCKED: isolation_failed** in any of these cases:
  - any other init problem;
  - a second failure;
  - plugins that are already on the block list. A relaunch can't help there.
- **Removed:**
  - the runner's own `blocked-plugins.json` code (`PLUGIN_BLOCKLIST`, `_blocked_plugins`, `_block_plugins`, which used a different `{"plugins": …}` shape);
  - the `plugin_keys=` parameter on `ChildrenPort.launch_writer/resume_writer`. children merges the persistent list into every launch itself.
- **Test fake:** `FakeChildren` now reports the keys children would block, read with `load_blocked_plugins`.
- **Test changes:** the relaunch test now asserts children's file shape, `{"plugin_keys": ["shiny@synced"]}`.

### 5. `preserves` and the criterion map
- **Stored criterion map:** it now has `children.TESTER_SCHEMA`'s shape:
  - `criteria[{id, text, preserves}]`;
  - `untestable[{criterion_id, reason}]`;
  - `tests`, `expected_missing`, `created` and `modified`.
- **Readers:** red-first and VERIFY build the map with `CriterionMap.from_tester_output(doc)`, so `preserving_criteria` reaches `classify_red_first`. `_tester_result` carries `TesterResult.preserves` into the implement, review and handoff prompts.
- **Removed:** the hand-built `_criterion_map`. It was equivalent apart from `preserves` and `from_tester_output`'s path normalisation.
- **Report:** it reads the new shape and lists "Covered by guard tests only (keep-working behaviour, R31)".
- **Test fixture:** `runner_fakes.TESTER_OUTPUT` carries `preserves: false`.
- **New test:** a C2 criterion marked preserves and covered by a passing guard on `test_add`. It checks:
  - the job reaches READY;
  - the stored flag;
  - red-first `guard_passes`;
  - the implement prompt's "(preserves existing behaviour …)";
  - the report rows.

### 6. `srt_version`
- **No change needed:** the runner makes no store call that takes `srt_version`. `doctor_needed` and `record_doctor_run` belong to doctor, which is T10.
- **For T10:** pass `children.SRT_VERSION` there, not `PreflightReport.srt_version`.
- **CLI evidence:** `record_cli_evidence` still stores the probed version, or `None` for repos without `install_command`, for the report.

## B. R32 rows

### 7. A guard failing on base is invalid (`reviews.redfirst` and the runner)
- **Classification:** `Verdict.GUARD_FAILS` (by assertion, by naming an `expected_missing` symbol, or at collection) is now in `INVALID_VERDICTS`, so `classify_red_first` returns `INVALID`.
  - `RedFirstResult.invalid` includes failing guards.
  - `retry_feedback()` explains them: "a guard test must pass on base, because it proves existing behaviour works (R32); it failed: …".
- **Runner:** it already retried the tester once with `retry_feedback()` and then raised BLOCKED: tester_invalid at RED_FIRST. It needed no change apart from the `tester_invalid` help text.
- **Tests:**
  - `test_redfirst.py`: the old test was renamed and inverted; added a lone failing guard and a failing guard on a `preserves` criterion.
  - Runner: a new BLOCKED case, a test that the retry prompt carries the failure, and a retry that succeeds.
- This also settles R31 report item 2 (a failing guard on a `preserves` criterion).

### 8. Codex "unjustified" harness judgement is a packet item (`reviews.coderabbit` and the runner)
- **`CodexReview`** gains `harness_judgements`. `codex_items` emits one item per `unjustified` judgement, and `justified` judgements stay in the report only. Each item has:
  - source `codex` and kind `harness_judgement`;
  - id `<head>/harness/<path>`;
  - a gaffer-worded body: the judged path, Codex's reason, and "Revert the change in a new commit (disposition fixed), or decline this item with the reason the change is needed".
- **New helper:** `reviews.harness_item_id`.
- **Runner:** it passes the recorded judgements through `codex_reviews=`. The item then goes through admission → round packet keys → PACKET → RESUME, like a finding, and uses a repair cycle.
  - The round comment counts the unjustified judgements.
  - The report marks them "(a packet item for the writer, R32)".
- **Tests:**
  - `test_codex_items.py`: admission, mappings, a new key per head, packet order.
  - Runner: the item reaches the resume prompt and uses a cycle, the writer declines it, and the report shows both the judgement and the disposition.

### 9. CLAUDE.md and AGENTS.md as guidance (`children`)
- **`read_repo_guidance(clone, scanner)`** reads `REPO_GUIDANCE_FILES = ("CLAUDE.md", "AGENTS.md")`.
  - Each file is opened safely as before (no symlinks, hard links or non-regular files, and a FIFO never blocks).
  - Each is scanned in full up to 1 MiB and capped at 16 KB on its own, with a per-file marker.
- **A scanner error** gives that file status `scan_failed`, no text, and the note "`<file>` was withheld from the writer: the secret scanner failed (`<ExceptionType>`)". The other file and the job carry on.
  - The note names only the exception type, because a scanner's message could quote the scanned text.
- **New types:**
  - `RepoGuidance` now holds `files: tuple[GuidanceFile, ...]` (name, status, text, size, note, rules);
  - `.included`, `.note` (joined) and `.file(name)`;
  - `to_dict()`/`from_dict()` for the runner's event.
- **Prompt section:** `### CLAUDE.md` and `### AGENTS.md`, each in its own nonce fence, or `(none: <note>)` for that file.
  - `prompts/implement.md` and `prompts/address.md` now say "CLAUDE.md and AGENTS.md … (each capped and secret-scanned)".
- **CLI flags unchanged** (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `--setting-sources user`). The existing test still checks both.
- **Tests:** the existing guidance tests now cover both files (symlink, hard-link, directory, FIFO and swapped-clone refusals are parametrized per file). New tests:
  - AGENTS.md in its own fence;
  - a secret in one file leaves the other included;
  - a scanner error drops one file with a note;
  - a dict round trip;
  - the runner test above, where the job reaches READY after a gitleaks error on CLAUDE.md.

## C. Adapter debt

### Cleaned up (the new interfaces made it trivial)
| Was | Now |
|---|---|
| The runner widened `store.BLOCKED_REASONS` at import (`RUNNER_BLOCKED_REASONS`) | `store.BLOCKED_REASONS` lists `isolation_failed`, `install_failed` and workspace's five codes itself |
| `job_marker()` rebuilt R31's dotted marker because `store.Job.ref` said `ENG-12-2` | `store.Job.ref` is `ENG-12` / `ENG-12.2` (R31); `job_marker()` returns it. Branch names are unchanged (`gaffer/ENG-12-2`) |
| `_STORE_ROLE` mapped children's `test` lease role to the store's `test_run` | The runner passes store role names (`test_run`) to `_child` |
| `_RV_TO_STORE_SOURCE`, hand-built item keys, runner-side Codex items, runner-side block list, `_criterion_map` | Removed (items 1, 2, 4, 5) |

### Remaining (not trivial; unchanged from the T9 report)
| Where | Adapter in runner.py | Implied change |
|---|---|---|
| store rounds | reviews' `RoundState` (seen keys, settle anchors, CodeRabbit state) kept as `round_state` events | a `state` JSON column on `rounds` |
| store jobs | the issue snapshot (`intake` event), `Workspace.to_record()` (`workspace` events) and now the repo guidance (`repo_guidance` event) live in the job log | `issue`/`workspace`/`guidance` JSON columns on `jobs` |
| reviews packet scanner | `PacketScanner` maps workspace `RawFinding(rule_id, …)` to packet `SecretFinding(rule, …)` | one shared SecretScanner protocol (children's guidance reader already accepts either) |
| reviews rounds | the runner calls `fetch_snapshot`/`evaluate_snapshot`/`Round.observe` itself, not `poll_round`, because it needs the PR snapshot for CLOSED, foreign_head and base_conflict | `poll_round` could return the snapshot |
| workspace install | `InstallRunner` → `children.run_install_command`; `InstallRequest.env` (TMPDIR, Composer vars, `npm_config_cache`) is dropped | children takes the extra env, or workspace stops building it |
| workspace private helpers | `_auth_env`, `_out`, `_status_paths`, `_resolve_commit`, `_is_ancestor` | expose `remote_branches()`, `fetch_and_adopt()`, `reset_to_base()` |
| config | `linear_token()` lives in runner.py | move it next to `github_token()` |
| prompt source labels | `_prompt_source` shows `check` as `ci` and `test` as `gaffer-verify` in prompts and comments | a display choice; keep or move to children's `PromptItem` docs |

## Test result

```
uv run pytest
863 passed in 377.55s (0:06:17)
```

The merged base had 846 tests (741 from the R31 amendments + 105 from T9), so this adds 17 net. They cover red-first under R32, Codex harness items, the two-file guidance reader (the refusal test is parametrized per file) and the runner integration (Codex findings exactly once, harness judgement items, guard-fails retry and block, preserves, guidance read once / after a crash / on a scanner error). gitleaks 8.30.1 is installed, so the real-gitleaks guidance test ran.

## Open issues

1. **Plugins already on the block list are BLOCKED: isolation_failed at once.** This applies when an init event shows plugins that are already in `blocked-plugins.json`, which means `enabledPlugins:false` didn't hold. There is no relaunch, because it couldn't help. R31 doesn't cover this case; it is my reading.
2. **Jobs whose writer started before this change** have no `repo_guidance` event. Their address and handoff prompts say "(none: gaffer did not read CLAUDE.md or AGENTS.md for this run)", and gaffer never reads the guidance after a writer has run. No real job exists yet, so this only matters for test databases.
3. **The stored criterion-map shape changed** from list pairs to the tester schema's objects. As with item 2, no real job exists yet. Any older `state.db` from manual tests would need `--fresh`, or deleting.
4. **The report's "Codex review of the head"** shows only the head's review. An unjustified judgement on an earlier head shows up in "Feedback items" with its disposition, not in that section.
5. **AGENTS.md doubles the guidance budget,** to at most 2 × 16 KB in each implement and address prompt.
6. **R19:** `prompts/implement.md` and `prompts/address.md` changed again before T11's eval baseline exists.
7. **Doctor:** T10 must pass `children.SRT_VERSION` to `doctor_needed`/`record_doctor_run` (item 6).
8. **Carried over from the T9 report** and not addressed here:
   - GitHub rate-limit headers aren't honoured;
   - a second `--more-cycles` after a crash double-grants (T6 item 7);
   - the install env is dropped (see the table);
   - `--adopt-head` after state_lost and `gaffer stop` aren't built.
