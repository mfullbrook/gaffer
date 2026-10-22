# T5 report: children

- **Branch:** `t5-children` (from `gaffer-v0.1-design`), not pushed.
- **Files:**
  - `src/gaffer/children.py`
  - `prompts/tester.md`, `prompts/implement.md`, `prompts/review.md`, `prompts/address.md` (and `prompts/.gitkeep` removed)
  - `tests/test_preflight.py`, `tests/test_children_launch.py`, `tests/children_fakes.py` (shared fakes and fixtures)
- **Verify:** `uv run pytest` gives 287 passed: T4's 173 plus 114 new (47 preflight, 67 launch). `uv run pytest tests/test_preflight.py tests/test_children_launch.py` runs only T5's.
- **No real calls.** Every `claude`, `codex`, `gitleaks`, `srt` and `node` in the tests is a small fake script. Each fake records its argv, env, cwd, pid, pgid and whether stdin was `/dev/null`, then replays canned output. That output is built from `spike/evidence/`: `r2_auth.txt`, the `a_claude_init.txt` init events (a1, a3, a8), `a_claude_json_schema.txt` and `d_tester_{py,js}.json`. Nothing touches `~/.claude`, `~/.codex` or `~/.gaffer`, because the tests use a temporary home. `config.py` is unchanged, and no dependency was added.

## What was built

### Preflight
`preflight(cfg, repo, paths=…)` collects every problem and then raises `PreflightError` (exit 4). It runs these checks:

- **R2 login.**
  - `claude auth status --json` must report `loggedIn: true`, `authMethod: "claude.ai"` and `apiProvider: "firstParty"`. If it reports a `configDirectory`, that must be `~/.gaffer/claude`. A missing field fails closed.
  - `codex login status` (stdout plus stderr, stripped) must be exactly `Logged in using ChatGPT`.
  - Both probes run in gaffer's config dirs with the stripped environment.
- **Refusals in gaffer's own environment:**
  - `CLAUDE_CODE_SIMPLE`, if present at all, even empty;
  - any non-empty `ANTHROPIC_*`, `OPENAI_*`, `AZURE_OPENAI_*` or `*_BASE_URL`;
  - the Bedrock, Vertex and Foundry switches, `CODEX_API_KEY` and `AWS_BEARER_TOKEN_BEDROCK`.
- **Claude settings files:**
  - Files checked: `/Library/Application Support/ClaudeCode/managed-settings.json`, every file in `managed-settings.d/*.json`, and gaffer's own `~/.gaffer/claude/settings{,.local}.json`.
  - Refused: an `env` block, `apiKeyHelper`, `awsCredentialExport`, `awsAuthRefresh`, `gcpAuthRefresh`, `forceLoginMethod: console`, or a file that can't be parsed.
- **Codex config:**
  - Files checked: `/etc/codex/managed_config.toml`, `/etc/codex/requirements.toml` and `~/.gaffer/codex/config.toml`, at any depth.
  - Refused: `model_provider(s)`, any `*base_url`, `env_key`, `experimental_bearer_token`, `mcp_servers`, an apikey login method, any `projects.*.trust_level`, or a file that can't be parsed.
- **Tools:**
  - gitleaks must be on the child PATH (R15).
  - For repos with an `install_command`: srt must sit at the pinned path with `package.json` version `0.0.79`, and `node` must be on PATH.
  - For PHP repos: Herd's `php` and `composer` must exist (R29).
- **MCP definitions:** every `allowed_mcp` name must be defined in `~/.gaffer/mcp.json`.
- **Recorded result:** a `PreflightReport` with the claude, codex and gitleaks versions, the srt version, the Claude status with emails, org and account fields redacted, the Codex login line, and the MCP servers that run outside the sandbox. The runner stores it per job (R2) and compares the versions for the doctor rerun (R18).

### Environment
`child_env()` builds each child's whole environment from scratch and passes it as `Popen(env=…)`, which is what `env -i` does.

- **Base keys for every child:** `HOME`, `PATH`, `USER`, `TMPDIR`, `LANG`, `TERM`, `CLAUDE_CONFIG_DIR=~/.gaffer/claude` and `CODEX_HOME=~/.gaffer/codex`.
- **PATH:** the spike's clean PATH (`~/.local/bin:~/.bun/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`).
- **Additions by role:**
  - **Writer:** the spike's `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` and the Bash timeout variables.
  - **Codex exec children:** `HOME=<job>/tmp/home`.
  - **PHP repos (writer, test and install):** Herd's bin dir goes first on PATH, plus `COMPOSER_HOME`, `COMPOSER_CACHE_DIR` and `COMPOSER_NO_INTERACTION` inside the job.
  - **srt:** keeps the system `TMPDIR` and sets `TERM=dumb`.
- **Fail-closed check:** any key outside the allowlist, or any forbidden key, raises an error before the launch.

### Claude writer settings
`writer_settings()` generates the `--settings` file:

- **Sandbox:** enabled, `failIfUnavailable`, `autoAllowBashIfSandboxed`, `allowUnsandboxedCommands: false`.
- **Writes and reads:** `allowWrite` is the job dir only, and `denyRead` is the full `config.deny_read_paths`, the vault included.
- **File-tool rules:** every deny entry gets both `Read(//<path>)` (an exact path, R29) and `Read(//<path>/**)`.
- **Network:** `allowedDomains` is the repo's `network_allowlist`, so a PHP repo gets the two GitHub archive hosts and never `github.com`.
- **Other denies:** WebFetch, WebSearch and the Linear and connector rules.
- **Lockdown:** `disableAllHooks`, `enabledPlugins` set to false for every known plugin key, and `disableBundledSkills` (R28).

### Codex profiles
`codex_profile_args()` builds the `-c` overrides from `spike/codex_profiles.sh`:

- **`gaffer_ro`** for the tester and the reviewer: reads everything except the deny list, writes nothing, no network.
- **`gaffer_test`** for `test_command`: adds write access to the job dir.

The tests parse the generated inline table with `tomllib`. A path containing a space (Herd) survives the quoting.

### Launch and resume
**`launch_writer(…, session_id=…)`** and **`resume_writer(…, session_id, prompt)`** use the spike's final flag set:

- `-p … --output-format stream-json --verbose --json-schema … --settings <state>/writer-settings.json --setting-sources user`;
- `--mcp-config … --strict-mcp-config --tools Bash,Read,Edit,Write,Skill --permission-mode acceptEdits`;
- `--model <writer_model> --max-turns N` and `--session-id <uuid>` or `--resume <uuid>`.

Further rules:

- The cwd is always `jobs.clone`, whatever gaffer's own cwd is (R9, R28).
- The session ID must be a UUID that the caller has already recorded. `new_session_id()` makes one.
- `--bare` is rejected in argv by the generic runner and by the probe runner.
- A prompt can't start with `-`.
- **The init check (R28).** gaffer reads stream-json line by line. On the init event it requires:
  - `plugins == []`;
  - every MCP server name in `allowed_mcp`;
  - `apiKeySource == "none"`.

  Any failure, or any non-system event before init, kills the process group at once and raises `InitCheckFailed` (exit 4) with the problems, the init event and the lease. The tests prove the fake never reaches its "act" step.
- `NUDGE_PROMPT` (R10) and `CONTINUE_PROMPT` (resume after a dead writer) are provided for the runner.

**Codex tester and reviewer.** `run_tester()` and `run_reviewer()` use the final flag set:

```
codex exec --json --ignore-rules <gaffer_ro> -c project_doc_max_bytes=0 -c skills.include_instructions=false
  -c web_search="disabled" --disable <10 features> [-m codex_model] --output-schema <state>/…-schema.json
  -o <state>/….result.json -C <clone> "<prompt>"
```

stdin is `/dev/null`.

**gaffer's own runs:**

- `run_test_command()` runs `codex sandbox <gaffer_test> -P gaffer_test -C <cwd> -- /bin/bash -c <test_command>`. The cwd must be inside the job dir. `command=` lets T8 run one PHPUnit file at a time.
- `run_install_command()` runs `srt --settings <state>/srt-….json -c 'export TMPDIR=<job>/tmp; cd <checkout> && <install_command>'`, with paths shell-quoted and the hosts defaulting to the repo allowlist. `hosts=()` gives the offline PHP worktree install (R29).

### Results (R5, R10, R22)
- **Schemas:** `TESTER_SCHEMA`, `WRITER_SCHEMA`, `ADDRESS_SCHEMA` and `REVIEW_SCHEMA` are strict: no extra properties, and every property required.
- **`parse_tester_result`** returns criteria, an `untestable` list with reasons, tests (criterion, `acceptance`/`guard`, `TestIdentity(file, classname, name)`), `expected_missing` and `files` (path and full contents). It checks:
  - unique ids, identities and paths;
  - that every reference resolves;
  - R5's "at least one acceptance test per testable criterion".

  An empty classname is accepted, because a pytest collection error has one. `testable_criteria` being empty means BLOCKED: no_testable_criteria.
- **`parse_writer_result`** returns `done` or `blocked`. `blocked` needs a question; `done` clears it. For address runs it also checks the dispositions:
  - exactly one per packet item;
  - `fixed` needs a 7–40 hex SHA;
  - `declined` needs a reason;
  - a `blocked` result may leave items open.

  Whether each SHA is a new commit on the branch needs git, so the caller checks it.
- **`parse_review_result`** returns findings (id, severity, path, line, title, body) and one `harness_judgements` entry per path (R7, R14).
- **When a result is missing or malformed**, `WriterRun.result_error`, `TesterRun.result_error` and `ReviewRun.result_error` are set instead of raising, so the runner picks the BLOCKED reason.

### Processes and limits
- **Process groups:** every child gets its own group (`start_new_session`) and stdin `/dev/null`.
- **Leases:** a `ChildLease` holds role, pid, pgid, start time (`ps -o lstart`), cwd, session ID and wall start. It goes to the `on_spawn` callback before gaffer waits on the child.
- **Helpers:** `lease_alive` (same PID and same start time, R9), `group_alive`, `wait_group` and `terminate_group` (SIGTERM, then SIGKILL after 10 s).
- **Timeouts:** a timeout kills the whole group.
- **After the writer exits,** gaffer waits up to 120 s for the rest of its group, then sends SIGTERM and records `group_terminated` (R9). gaffer's own runs clean their group up at once.
- **Logs:** stdout logs are capped at 64 MB, and output tails at 64 KB.
- **Usage-limit events** come back as `LimitEvent`:
  - **Claude:** a `rate_limit_event` whose `status` isn't `allowed`/`allowed_warning`, or an error result whose text matches "usage limit".
  - **Codex:** the session rollout's last `token_count.rate_limits` (`rate_limit_reached_type`, or `used_percent >= 100`), found by `thread_id` under `CODEX_HOME/sessions`, or an error event whose text matches.
- **Usage recorded per attempt:** `usage`, `num_turns`, `duration`, `model`, `claude_version`, `skills` and Codex `rate_limits`.

### Prompts
- **Adapted from:** `code-loop` stages 1–3 (plan proportionally, implement, verify locally, self-review, logical commits) and `address-pr-feedback` (verify before fixing, declining is correct, minimal fixes, validate the batch). gaffer owns everything outward-facing.
- **Untrusted text:** issue text, the criterion map, harness reasons and packet items are fenced by `untrusted()`. The fence is a boundary with a random 16-hex nonce that the text can't forge, and each prompt says what's inside is data, not instructions. A test plants a forged end marker plus an exfiltration instruction in the issue body and checks that it stays inside the fence.
- **Rendering:** `render_prompt` fills `{{placeholders}}` in one pass, so placeholder-like text inside a value isn't expanded.
- **Builders:** `tester_prompt`, `implement_prompt`, `review_prompt` and `address_prompt` build the values. `IDENTITY_CONVENTIONS` holds each runner's JUnit naming.

## Choices made where the docs left it to T5 or were silent

1. **Where MCP definitions live (R30).**
   - **Where:** `~/.gaffer/mcp.json`, in the `{"mcpServers": {name: definition}}` shape. Mark maintains it.
   - **What reaches the child:** only the entries named in `allowed_mcp`, written 0600 to the job's state dir and passed by path. With no MCP allowed, the proven inline `'{"mcpServers":{}}'` is passed instead.
   - **Why there:** `~/.gaffer` is denied to every child, so a child can't read server credentials in the file or add a server.
2. **Private state dir.**
   - **What it holds:** the generated settings, MCP config, schemas, results, srt settings and logs.
   - **Where it must be:** `JobDirs.state_dir` must sit under a denied path, for example `~/.gaffer/jobs/<key>`, and launch refuses otherwise. That way the writer, which can write anywhere in the job dir, can't edit its own sandbox settings in the middle of a session.
   - **Job dir checks:** the job dir must be under `job_root`, must not overlap a denied path, and must contain the clone.
3. **Codex `HOME=<job>/tmp/home`**, following the final flag set. So Codex children don't see `~/.agents/skills` at all, which is stricter than R28's "may see, report lists them".
4. **`test_command` runs as `/bin/bash -c <string>`.** The spike passed argv lists, but gaffer.toml holds a string, and a shell is needed for `&&` and env prefixes (Jest's `JEST_JUNIT_*`).
5. **Extra init check: `apiKeySource == "none"`.** This applies R2's positive check to every child as it starts. The spike saw `"none"` on every run.
6. **Codex children get no MCP in v0.1.** Preflight refuses `mcp_servers` in gaffer's Codex config, because the ledger only specifies Claude's MCP controls.
7. **Limits with no config field:**
   - writer: 2 h wall time and 80 turns;
   - tester and reviewer: 20 min each;
   - install: 15 min;
   - test: the repo's `test_timeout`;
   - probes: 60 s.

   All are function parameters with these defaults.

## Interfaces for T9 to wire, and what T5 needs from the other modules

**T5 exposes:**
- `Paths`, `JobDirs`
- `preflight`, `PreflightReport`, `PreflightError`
- `new_session_id`, `launch_writer`, `resume_writer`, `WriterRun`, `NUDGE_PROMPT`, `CONTINUE_PROMPT`
- `InitCheckFailed`, `plugin_keys_from_init`
- `run_tester`/`TesterRun`, `run_reviewer`/`ReviewRun`
- `run_test_command`, `run_install_command`, `CommandRun`
- `ChildLease`, `lease_alive`, `group_alive`, `wait_group`, `terminate_group`
- `LimitEvent`
- `parse_*_result`, `TesterResult`, `TestIdentity`, `WriterResult`, `Disposition`, `ReviewResult`
- `tester_prompt`, `implement_prompt`, `review_prompt`, `address_prompt`, `PromptItem`

**Store (T6):**
- Persist the writer's session ID **before** calling `launch_writer`.
- Persist each `ChildLease` from `on_spawn`: role, pid, pgid, start_time, cwd, session_id.
- Persist the `PreflightReport` per job and the per-attempt usage fields.
- T5 doesn't import store.

**Workspace (T7):**
- **Job layout:** the clone and the verification worktrees live inside the job dir under `job_root`. T5 creates `tmp/`, `tmp/home/` and `composer-home/cache/` in the job dir, so T7 must not use those names.
- **Commit identity:** T7 sets `user.name` and `user.email` in the clone (R28).
- **Tester files:** T7 validates `TesterResult.files` paths (R6: realpath, symlinks, `..`, test_dirs, 20 files, 200 KB). T5 checks only shape.
- **Dispositions:** T7 or T9 checks that each `Disposition.commit_sha` is a new commit since the packet head (R10).
- **gitleaks:** T7's gate scans `CommandRun.output_tail` and test output before they enter a packet (R24).

**Reviews (T8):**
- The adapters must agree with `TestIdentity(file, classname, name)` and with the conventions in `IDENTITY_CONVENTIONS`, which are worded in the tester prompt. T8 could own that text.
- T8 runs per-file PHPUnit through `run_test_command(command=…)`.
- T8 turns `ReviewFinding`s and `HarnessJudgement`s into packet items, and T9 maps packet items to `PromptItem(id, source, location, body)` for `address_prompt`.

**Runner (T9):**
- On `InitCheckFailed` for plugins: add `plugin_keys_from_init(e.init)` to `plugin_keys` and relaunch **with a new session ID**, since the killed child may already have created the old one. Persisting the learned plugin keys belongs to T9 or T10.
- Map `result_error` and `limit` to BLOCKED reasons.

## Decisions that look wrong or are missing

1. **R5's coverage rule rejects the spike's own tester output.**
   - **The problem:** R5 says "at least one acceptance test per testable criterion". Both recorded tester outputs (`d_tester_py.json`, `d_tester_js.json`) have a guard-only criterion, C3 "shout keeps upper-casing inner text", and such a criterion can't have a test that fails on the base.
   - **What I did:** the code follows R5 as written. A test (`test_recorded_spike_output_trips_the_r5_coverage_rule`) records the conflict, and the other tests attach C3's guard test to C2.
   - **Likely consequence:** real tickets with "must keep working" criteria end as tester_invalid.
   - **Suggested fix:** amend R5 to "at least one acceptance test per testable criterion that changes behaviour; a preservation criterion needs at least one guard test".
2. **gitleaks isn't installed on this Mac** (`which gitleaks` finds nothing). Preflight will refuse every run until `brew install gitleaks` (R15).
3. **The plugin lockdown is a denylist.**
   - **Why:** no setting was found that disables all plugins, so `enabledPlugins: false` covers only known keys (the three built-ins plus `cowork-plugin-management@synced`).
   - **Effect:** a newly synced plugin makes the init check kill the child (safe), and the runner relaunches once with the learned key.
   - **Open:** the runner needs a rule for when the relaunch also fails, and someone needs to decide whether learned keys persist (T9 or T10).
4. **No gaffer.toml fields for writer, tester, reviewer or install timeouts, or for writer max turns.** The defaults are in "Choices" above. If they should be per repo, config needs fields (T4 frozen; not changed).
5. **`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` conflicts with the design.** The proven flag set turns CLAUDE.md off completely, while the design says CLAUDE.md is "read as untrusted guidance". I followed the flag set, so the writer doesn't see repo conventions from CLAUDE.md. Decide whether that's wanted.
6. **Unproven combinations, which the R18 doctor should probe:**
   - `--resume` together with `--json-schema`: a fresh launch with a schema and a resume without one were each proven, but not both together.
   - the shape of `--json-schema` as an inline JSON string (from CLI docs);
   - the Vitest identity convention;
   - the raw shape of `plugins` and `mcp_servers` in the init event: the evidence keeps only extracts, so both strings and `{name, source}` objects are handled;
   - limit-event shapes during a real limit hit;
   - the paths of Claude's `managed-settings.d` and Codex's `/etc/codex/{managed_config,requirements}.toml`;
   - macOS MDM-managed Codex preferences, which preflight does not inspect.
7. **The R9 group wait runs inside `launch_writer`/`resume_writer`.** If a writer leaves background processes behind, a call can take up to 2 extra minutes. Pass `group_grace` to change it.
8. **Codex exec has no turn limit;** only the wall timeout bounds it.
9. **`srt` takes gaffer's own `TMPDIR`,** falling back to `/tmp` only if it's unset. If gaffer is ever started with a long `TMPDIR`, srt's 104-byte socket path limit would bite. One option is to always use `/tmp` for srt.
10. **Prompts and evals:**
    - The prompts were written before the R19 eval exists (T11). Store the eval baseline before the first edit.
    - The `prompts/` dir is found relative to the source tree, so a wheel install wouldn't include it. Distribution is Approach B, but packaging will need a decision.
11. **Prompt and settings caveats:**
    - Prompts and issue text go in argv, so they're visible to `ps` and bounded by ARG_MAX (1 MB). That's fine for ticket-sized text.
    - Edit and Write rules aren't added for the deny list. Writes outside the job dir rely on `acceptEdits` refusing paths outside the cwd in `-p` mode (spike B) plus the Bash sandbox, as the ledger specifies.
