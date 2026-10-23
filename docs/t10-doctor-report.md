# T10: `gaffer doctor` (R18)

Built 2026-10-23 on branch `t10-doctor`, which was cut from `gaffer-v0.1-design` at 0c0ab6c. Decisions in scope:

- R18 decision A: auto doctor.
- R20: Codex-child and test-run read denial.
- R28: the deny list, plugins and skills, sandbox-runtime, Codex `web_search`.
- R29: exact-path file-tool Read rules, Composer and Herd deny entries, the srt pin.
- R31: rerun keyed on the srt pin, and the plugin block list.
- R2: subscription logins only.

**Result:**

- `uv run gaffer doctor` **passed all 22 probes** against Claude Code 2.1.308, codex-cli 0.160.0 and sandbox-runtime 0.0.79. Claude Code moved from 2.1.304 to 2.1.308 since the spike, and every control still holds.
- Test suite: `tests/test_doctor.py` has 100 tests. Full suite: **963 passed** (863 before plus 100 new) in 7m41s.
- Every key assertion was mutation-checked: 25 mutants, all caught.

## What was built

| File | What |
|---|---|
| `src/gaffer/doctor.py` (new) | The probe suite: `run_doctor`, `record`, `stale`/`needed`, `format_report`, `refusal`, `scrub`. A separate module, because `children.py` is already 2,000 lines. It reuses children's launch pieces unchanged: `writer_argv`, `writer_settings`, `child_env`, `codex_exec_argv`, `codex_profile_args`, `sandboxed_test_argv`, `srt_settings`, `install_argv`, `_run_child`, `ClaudeStream`/`init_problems`, `env_problems`, the settings/config checkers, `redact`, `record_blocked_plugins` and `SRT_VERSION`. Each probe therefore exercises the exact flags production launches with. |
| `src/gaffer/runner.py` | `ChildrenPort.doctor()` / `RealChildren.doctor()`.<br>`Runner._doctor()` runs right after preflight and before any job record, child or GitHub call. When `doctor.stale()` gives a reason, it prints the reason, runs doctor, records the result, prints the report, and on failure raises `Refused`. `Refused` is exit 4, and its message names every failing probe with its evidence and fix.<br>New `gaffer doctor [--config PATH]` subcommand (`doctor_main`): exit 0 on pass, 4 on any failure (R8 convention), 4 for an invalid or missing explicit config, 4 while `run.lock` is held. |
| `src/gaffer/__init__.py` | Docstring lists the new subcommand and its exit codes. |
| `tests/doctor_fakes.py`, `tests/test_doctor.py` (new) | Fake `claude`/`codex`/`srt`/`node`. Each one reads the probe script and prompt gaffer wrote and answers like a working sandbox, unless told otherwise. A write the fake "allows" really creates the file, so stray-file detection is exercised. |
| `tests/runner_fakes.py` | `FakeChildren.doctor()`: passes by default, `doctor_failures` makes named probes fail, `versions` sets what preflight reports. |

### Config: how doctor runs without gaffer.toml

Mark hasn't written `~/.gaffer/gaffer.toml`, so doctor handles both cases:

- **`gaffer doctor` with no `--config` and no `~/.gaffer/gaffer.toml`** uses built-in defaults:
  - the R28/R29 deny list (`config.DENY_READ_HOME_PATHS`);
  - **a stand-in vault dir** (a temp dir holding a canary) in place of `vault_path`;
  - a fresh temp dir as the job root.

  It says so on the first line of output. This is how the live run below ran. Nothing was written to `~/.gaffer/gaffer.toml`.
- **`--config PATH`** or an existing `~/.gaffer/gaffer.toml`:
  - uses that config's deny list (the real vault included), `allowed_mcp`, `codex_model` and `job_root`;
  - creates the temp job dir under `job_root`;
  - probes the real vault with `ls` and a file-tool Read of the directory path, and never writes into it.

**Stale defaults pass.** A pass recorded under defaults doesn't satisfy `gaffer run` once a config exists. The stored results carry the deny list they proved, and a different list reruns doctor (see Decisions, item 1).

## Probe list

Every probe result has: name, pass/fail, short evidence and, on failure, a fix line. Scripts never print file contents: a file read is `head -c 1 > /dev/null`, and a directory read is `ls`. Each result line carries a per-run random nonce, so only the script can produce one.

Each run builds this layout:

- `<root>/job/repo`: a git-initialised job clone, the cwd of every child.
- `<root>/outside`: not denied by any rule, so writes there must still fail.
- `~/.gaffer/doctor/<id>`: gaffer-private settings, logs and a canary inside the denied `~/.gaffer`. Deleted after a pass. After a failure it's kept for debugging, with the canaries removed.

| # | Probe | Surface and cost | What it proves |
|---|---|---|---|
| 1 | `env` | none | No `ANTHROPIC_*`, `OPENAI_*`, `*_BASE_URL`, provider switch or `CLAUDE_CODE_SIMPLE` in gaffer's env (R2). |
| 2 | `versions` | `--version` | Both CLIs are on the child PATH. Records the version strings exactly as preflight does. |
| 3 | `claude_auth` | `claude auth status --json` | `loggedIn`, `authMethod "claude.ai"`, `apiProvider "firstParty"`, `configDirectory == ~/.gaffer/claude`, with the subscription type reported. Port of `spike/r2_auth.sh`. |
| 4 | `codex_auth` | `codex login status` | Exactly `Logged in using ChatGPT` in `~/.gaffer/codex` (an API-key login fails). |
| 5 | `config_dirs` | files only | Both gaffer dirs exist and aren't `~/.claude`/`~/.codex`. Managed and gaffer-user settings set no env block, key helper, provider, MCP server or trusted project. `mcp.json` and the plugin block list parse. |
| 6 | `claude_isolation` | **one `claude -p`** (haiku, writer flag set and settings, `acceptEdits`, empty `--mcp-config` + `--strict-mcp-config`) | The init event shows:<br>• `plugins == []`, `mcp_servers == []`, no `mcp__*` tools;<br>• tools ⊆ writer set + StructuredOutput;<br>• `apiKeySource none`, `permissionMode acceptEdits`;<br>• account-synced skills listed and accepted (R28).<br>The session transcript lands in `~/.gaffer/claude/projects` and not `~/.claude/projects`. A plugin-only init failure is recorded in the block list and relaunched once (R31). |
| 7 | `claude_read` | same child, Bash tool | The Bash sandbox denies:<br>• reading every existing deny entry: `ls` for dirs (`~/.ssh`, `~/.config/gh`, `~/Library/Keychains`, `~/.claude`, `~/.codex`, `~/.gaffer`, `~/.composer`, `~/.config/composer`, Herd valet, vault) and `head -c 1` for `~/.claude.json`;<br>• reading every known credential file that exists (SSH keys and config, gh hosts, login keychain, both Claude and both Codex credential files, Herd CA key);<br>• reading or `stat`ing the canaries.<br>A job file stays readable (positive control). |
| 8 | `claude_file_read` | same child, Read tool | **R29:** a Read of every existing deny entry path (the exact-path rule, which for `~/.claude.json` is the only rule that can match) and of the canaries inside `~/.gaffer` and the vault (the glob rule). Each must be refused with "denied by your permission settings". Under `acceptEdits` an unmatched path gets "you haven't granted it yet" instead, so the rule itself is proven without `bypassPermissions` and without ever reading a real credential. |
| 9 | `claude_write` | same child | Bash can write the job dir. Bash can't write `<root>/outside`, `$HOME` or `~/.gaffer`. The Write tool is refused outside the job dir. No stray file exists afterwards: any that does is deleted and named. |
| 10 | `claude_network` | same child | `pypi.org` answers (the allowlist works). `example.com`, `github.com` and `api.github.com` are blocked (curl `000`). |
| 11 | `codex_isolation` | **one `codex exec`** (tester/reviewer flag set unchanged, plus `model_reasoning_effort="low"`) | Exit 0, no `mcp_tool_call`. The rollout is in `~/.gaffer/codex/sessions`, not `~/.codex`. Its `world_state` shows `agents_md` empty and skills, plugins and apps instructions off (spike A5). |
| 12 | `codex_read` | same child, shell tool | As `claude_read`, under the `gaffer_ro` profile (R20). |
| 13 | `codex_write` | same child | `gaffer_ro` writes nothing, not even the job dir. |
| 14 | `codex_network` | same child | No network at all, `pypi.org` included. |
| 15 | `codex_web_search` | same child | **R28 "still unproven":** with `web_search="disabled"`, no `web_search` item appears and the child's structured answer says it has no web search tool. |
| 16-18 | `test_read`, `test_write`, `test_network` | `codex sandbox -P gaffer_test` (no model) | test_command's sandbox (R4, R20): credentials denied, the job dir writable (JUnit) and nothing else, no network. |
| 19 | `srt_install` | files | sandbox-runtime is at `~/.gaffer/tools/...`, its version equals the pin 0.0.79, and `node` is on PATH. The fix line is the pinned `npm install`. |
| 20-22 | `srt_read`, `srt_write`, `srt_network` | `srt` with `children.srt_settings` (no model) | install_command's sandbox (R14, R28, R29): credentials denied, the job dir writable, `pypi.org` reachable, other hosts blocked. |

**Web search proof.** The live probe found that the only reliable signal is behavioural: the rollout records no tool list. I ran a **positive control by hand** once, with the same prompt and `web_search="live"`. That run produced an `item.completed` of type `web_search` and the child answered `web_search_tool_available: true`. With `"disabled"` (production) there was no such item and the answer was `false`. Doctor runs only the production case each time (Decisions, item 4).

**Gating (R2).** No model child is ever launched while `env`, its CLI's auth probe or `config_dirs` is failing: an API-key login or a key helper could bill an API key. Those probes fail as `not run: <gate> failed` with a fix line. They aren't skipped. The no-model sandboxes need only `env` plus the binary (`srt_install` for srt).

**Crashes.** An exception in any probe group fails every probe of that group still without a result (`probe crashed: <Type>: <redacted message>`). The remaining groups still run.

**Storage.**

- `record()` writes `store.record_doctor_run(claude_version, codex_version, srt_version=children.SRT_VERSION, passed, results)`.
- `results` holds the versions, the installed srt, the config used, `suite`, `deny_paths` and every probe's `{name, passed, evidence, fix}`.
- Everything stored passes through `scrub()`: `children.redact` (emails; email/org/account/user keys) plus a token pattern (`sk-`, `gh?_`, `xox?-`, JWTs, 40+ char base64-like runs).
- The stored row from the live run has no email and no token.

**Preflight trigger (`doctor.stale`).** A rerun is triggered by any of:

- **R18/R31:** the Claude version, the Codex version or the srt pin differs from the last pass (`store.doctor_needed`).
- **Added, conservative:** the deny list differs from the one the last pass proved.
- **Added, conservative:** `SUITE_VERSION` changed.

A failed doctor run is recorded but never counts as a pass, so the next run tries again.

## Tests

`tests/test_doctor.py`: 100 tests, all passing (`uv run pytest tests/test_doctor.py`, about 95 s).

- **Whole suite:** all 22 pass against working fakes, in `PROBES` order, with one Claude child and one Codex child. The Claude argv is the writer flag set with `--model haiku`, an empty strict MCP config, no `--bare` and no `ANTHROPIC_*`. The Codex argv carries the tester flag set plus low effort, and the test sandbox uses `gaffer_test`. Probe dirs are disposable and removed. With a config, the job dir is under `job_root` and the real vault is probed but never written.
- **Fail paths, one or more per probe:**
  - API key in env (no child starts, key not stored);
  - missing claude;
  - four Claude login states;
  - Codex API-key login (no Codex child; `codex sandbox` probes still run);
  - `apiKeyHelper` and a trusted project in config;
  - plugin relaunch: once, recorded, a fresh session ID;
  - plugin twice;
  - Linear MCP or a key source in init;
  - connector tools, extra tools, permission mode;
  - transcript missing or in `~/.claude`;
  - a readable credential (4 variants);
  - an unreadable job file (vacuity guard);
  - network (4 variants);
  - writes outside the job (3 variants, cleaned up);
  - unwritable job dir;
  - Write tool succeeding;
  - a file-tool Read that succeeds (4 variants; contents never stored);
  - a file-tool Read refused only for lack of a grant (R29);
  - every existing deny entry is Read-probed and absent ones aren't;
  - a skipped step gets one fresh attempt; skipped twice fails as "not attempted";
  - a real failure plus a skipped step is never retried;
  - skipped Bash step;
  - Codex child read, write and network (7 variants);
  - Codex web-search event; the child admitting a web search tool; no structured result (retried once, then fails);
  - skipped Codex script (retried);
  - Codex isolation (7 variants);
  - test sandbox (5 variants), srt sandbox (5 variants), srt missing, srt off the pin.
- **Crash and storage:**
  - a crashing group fails its probes and redacts the message;
  - a crash after partial results keeps them;
  - stored results are redacted, carry all three versions and keep `doctor_needed` true after a failure;
  - a pass satisfies `needed` until a version changes;
  - `scrub` keeps paths intact.
- **Preflight (`gaffer run` with `runner_fakes.World`):**
  - the first run runs doctor and records the pass;
  - no rerun when versions match;
  - a rerun on a Claude version change, a Codex version change, and an srt pin change (`children.SRT_VERSION` monkeypatched);
  - a failed doctor gives exit 4 with `doctor failed (claude_file_read; R18)` and the evidence, and no job is created;
  - a failed doctor reruns next time, and a pass lets the run proceed;
  - a matching pass is reused;
  - a defaults-mode pass reruns once a config exists;
  - an older suite reruns.
- **CLI:**
  - `gaffer doctor` exits 0 on a pass and records it;
  - exit 4 on failure, naming the probe;
  - `--config` is used;
  - a missing `--config` file gives exit 4;
  - exit 4 while `run.lock` is held;
  - the parser has the subcommand.

**Full suite.** Baseline on 0c0ab6c: 863 passed in 6m24s. After this change: **963 passed in 7m41s**. The only change to existing tests is in `runner_fakes.FakeChildren`, which gains `doctor()`. Every existing runner test now goes through the doctor step with a passing fake.

## Mutation checks

Each mutant was applied to the source, the targeted tests were run, and the original was restored. The file was compared with `cmp` against a saved copy afterwards.

| Mutant | Result |
|---|---|
| network "blocked" accepts any answer | caught |
| file-tool Read: any refusal counts as the deny rule | caught |
| preflight ignores a failed doctor (no `Refused`) | caught |
| doctor keyed on a constant srt version | caught |
| no redaction in `scrub` | caught |
| Claude child launched without the auth/env gate | caught |
| a crashing probe is skipped (handler narrowed) | caught |
| stray writes not detected | caught |
| transcript location not checked | caught |
| `web_search` events ignored | caught |
| real Claude control failures retried | caught |
| real Codex control failures retried | caught |
| doctor rerun skipped in preflight | caught |
| Codex probe allowed to write the job dir | caught |
| R31 plugin relaunch disabled | caught |
| Codex `world_state` not checked | caught |
| srt pin not checked | caught |
| Claude `authMethod` not checked | caught |
| doctor result not recorded by `gaffer run` | caught |
| vacuity guard (readable job file) removed | caught |
| deny list not compared in `stale` | caught |
| suite version not compared in `stale` | caught |
| (3 more variants of the above while iterating) | caught |

One mutant that only rewrote `relaunched = () or …` was equivalent to the original, so it couldn't fail. I dropped it.

## Live run

`uv run gaffer doctor` ran 2026-10-23 07:39 on Mark's Mac with no gaffer.toml, so under defaults. Paths: `~` = home, `<doctor>` = the disposable temp root. Long evidence is cut at 400 characters by design.

```
gaffer doctor: no /Users/mark/.gaffer/gaffer.toml; probing under built-in defaults (the vault entry is a stand-in dir).
gaffer doctor: 2.1.308 (Claude Code); codex-cli 0.160.0; sandbox-runtime 0.0.79 (pinned 0.0.79); config: none (defaults; the vault entry is a stand-in)
  PASS  env               no API-key, provider or base-URL variables set; CLAUDE_CODE_SIMPLE unset
  PASS  versions          2.1.308 (Claude Code), codex-cli 0.160.0
  PASS  claude_auth       loggedIn, authMethod claude.ai, apiProvider firstParty, subscription pro, config dir ~/.gaffer/claude
  PASS  codex_auth        Logged in using ChatGPT in ~/.gaffer/codex
  PASS  config_dirs       ~/.gaffer/claude and ~/.gaffer/codex; managed and user settings set no env, key helper, provider, MCP or trusted project
  PASS  claude_isolation  init: plugins [], mcp_servers [], no connector tools, apiKeySource none, claude-haiku-5-5; transcript in ~/.gaffer/claude; 17 synced skills accepted (R28): code-loop, design, doctor, anthropic-skills:built-in-browser, anthropic-skills:chrome-browser, anthropic-skills:computer-use, anthropic-skills:deep-research, anthropic-skills:docs, anthropic-skills:do
  PASS  claude_read       25 checks as expected: DENIED: list ~/.ssh, list ~/.config/gh, list ~/Library/Keychains, list ~/.claude, list ~/.codex, list ~/.gaffer, read ~/.claude.json, list ~/.composer, list ~/.config/composer, list ~/Library/Application Support/Herd/config/valet, list <doctor>/vault, read ~/.ssh/id_ed25519, read ~/.ssh/config, read ~/.config/gh/hosts.yml, read ~/Library/Keychains/login.keychain-db, read ~/.claude/.credentials....
  PASS  claude_file_read  Read tool denied by permission rules for all 13: ~/.ssh, ~/.config/gh, ~/Library/Keychains, ~/.claude, ~/.codex, ~/.gaffer, ~/.claude.json, ~/.composer, ~/.config/composer, ~/Library/Application Support/Herd/config/valet, <doctor>/vault, ~/.gaffer/doctor/20261023T073938-be02d3/canary/token, <doctor>/vault/canary-note.md
  PASS  claude_write      4 checks as expected: ALLOWED: write <doctor>/job/.doctor-claude_write-…; DENIED: write <doctor>/outside/doctor-claude_write-…, write ~/.gaffer-doctor-claude_write-…, write ~/.gaffer/doctor/…/doctor-claude_write-…; Write tool refused at <doctor>/outside/doctor-write-tool-….txt
  PASS  claude_network    4 checks as expected: reachable: https://pypi.org/simple/pip/; blocked: https://example.com/, https://github.com/, https://api.github.com/zen
  PASS  codex_isolation   rollout in ~/.gaffer/codex/sessions; no MCP calls; AGENTS.md, skills, plugins and apps instructions off
  PASS  codex_read        25 checks as expected: DENIED: (same 25 as claude_read)
  PASS  codex_write       4 checks as expected: DENIED: write <doctor>/job/…, write <doctor>/outside/…, write ~/.gaffer-doctor-…, write ~/.gaffer/doctor/…
  PASS  codex_network     4 checks as expected: blocked: https://pypi.org/simple/pip/, https://example.com/, https://github.com/, https://api.github.com/zen
  PASS  codex_web_search  no web_search events; the child reports no web search tool
  PASS  test_read         25 checks as expected: DENIED: (same 25)
  PASS  test_write        4 checks as expected: ALLOWED: write <doctor>/job/…; DENIED: outside, ~/.gaffer-doctor-…, ~/.gaffer/doctor/…
  PASS  test_network      4 checks as expected: blocked: https://pypi.org/simple/pip/, https://example.com/, https://github.com/, https://api.github.com/zen
  PASS  srt_install       sandbox-runtime 0.0.79 at ~/.gaffer/tools/sandbox-runtime/node_modules/.bin/srt
  PASS  srt_read          25 checks as expected: DENIED: (same 25)
  PASS  srt_write         4 checks as expected: ALLOWED: write <doctor>/job/…; DENIED: outside, ~/.gaffer-doctor-…, ~/.gaffer/doctor/…
  PASS  srt_network       4 checks as expected: reachable: https://pypi.org/simple/pip/; blocked: https://example.com/, https://github.com/, https://api.github.com/zen
gaffer doctor: passed (22 probes).
gaffer doctor: recorded in /Users/mark/.gaffer/state.db.
exit=0
```

The run took about a minute. I checked afterwards:

- `~/.gaffer/state.db` has the pass: `doctor_runs` row 2, (2.1.308, 0.160.0, 0.0.79), 22/22, no email or token in `results`.
- `doctor_needed` is false for these versions.
- No `~/.gaffer-doctor-*` file exists in `$HOME`.
- `~/.gaffer/doctor/` is empty.
- `~/.claude`, `~/.codex` and gaffer.toml were not touched.

### Probe bugs found live and fixed (not isolation gaps)

1. **Haiku skipped the Read step.** The first prompt put the Reads second, and the model returned after the Bash and Write steps. Doctor reported `claude_file_read` FAIL with "not attempted", which is correct but was a probe bug. Fixes:
   - the Reads now go first, as one message, with the expected tool-call count stated;
   - when the only problem is a skipped step, doctor retries once with a fresh session;
   - a control that actually failed is never retried.
2. **Shared write targets across surfaces.** The Claude probe's allowed job-dir write was later counted as a stray Codex write. Write targets are now per surface (`.doctor-<probe>-<nonce>`).
3. **Over-eager token scrub.** The 40+ character catch-all treated long probe file names as tokens. It's now limited to base64-like runs with no `-`/`_` separators, and a test pins that paths survive.
4. **`codex exec` refused to start outside a git repo** ("Not inside a trusted directory"). Fixed by having the doctor job clone `git init`ed, as a real job clone is. No trust entry is ever written.

**Earlier record.** An earlier live run of the same code before the `deny_paths`/`suite` fields existed also passed 22/22 (row 1). It no longer satisfies preflight because it lacks those fields, which is the intended behaviour.

**Quota used for this task.** Claude: 4 haiku probe children, a few turns each, between 13k and 20k tokens mostly cached. Codex: 4 `codex exec` runs at low effort, including the one-off `web_search="live"` positive control. Every `codex sandbox` and `srt` run is free.

## Decisions for Mark

Each item below is a choice the ledger doesn't cover. Where one was needed, I took the conservative option.

1. **Doctor reruns on more than versions.** R18/R31 key the rerun on the Claude, Codex and srt versions. I also rerun it when:
   - **the deny list differs from the one the last pass proved.** Today's pass ran under defaults with a stand-in vault, so the first `gaffer run` after Mark writes gaffer.toml will rerun doctor against the real `vault_path`.
   - **`doctor.SUITE_VERSION` changes**, which closes the r31 report's item 9: changing the probe suite didn't trigger a rerun.

   Both are extra reruns, never fewer. Accept, or drop back to versions only.
2. **No-config mode.** `gaffer doctor` works without gaffer.toml, using defaults and a stand-in vault. `--config PATH` points it at any config. Doctor never creates gaffer.toml.
3. **Cheapest models.**
   - The Claude probe uses `--model haiku`, not `writer_model`. Sandbox and permission enforcement live in the CLI, not the model, so the model only has to follow the steps.
   - The Codex probe uses `codex_model` or the CLI default, plus `model_reasoning_effort="low"`. Not "minimal", which can drop web search and would make that probe vacuous.

   If you want the probe to run on the production writer model, it costs more per version change.
4. **Web search is proven behaviourally, with no positive control per run.** Each run requires no `web_search` event and a "no tool" self-report. The positive control (`web_search="live"` → a `web_search` item) was run once by hand and is not repeated each time, to save Codex quota. Adding it per run costs one more `codex exec`.
5. **One retry for a skipped step, never for a failed control.** Models sometimes skip a step. Without the retry, the night run would refuse on model flakiness. Any check with an actual wrong outcome blocks the retry, and a test plus a mutant pin that.
6. **R31 applies inside doctor.** A newly synced plugin is recorded in `~/.gaffer/blocked-plugins.json` and doctor relaunches once. Otherwise a synced plugin would fail doctor forever, and `gaffer run` (the only other place that records it) could never get past preflight. A second failure fails `claude_isolation`.
7. **srt is always required.** Doctor probes srt even when no repo has an `install_command` (R28: "doctor probes it"). A missing or off-pin srt fails doctor and therefore `gaffer run` on the next version change.
8. **Network positive control.** `pypi.org` must answer under the Claude and srt sandboxes. Without it, "blocked" proves nothing (no network at all looks the same). So doctor fails when the Mac is offline, and a version change while offline blocks that night's run at preflight.
9. **Doctor passes no MCP servers.** The doctor child gets `{"mcpServers":{}}` and requires `mcp_servers == []`, even if `allowed_mcp` is set. The allowlisted servers themselves are not exercised by doctor.
10. **`gaffer doctor` takes `run.lock`.** It refuses with exit 4 while a `gaffer run` is in progress, and vice versa.
11. **Leftovers.** Doctor's Claude transcripts and Codex rollouts stay in `~/.gaffer/claude/projects` and `~/.gaffer/codex/sessions`, like any child's. Settings and logs under `~/.gaffer/doctor/<id>` are deleted on a pass and kept on a failure, with the canaries removed.

## Open issues

- **T5 report item 6 is still not probed:**
  - `--resume` with `--json-schema` (would cost a resume turn);
  - the Vitest identity convention;
  - limit-event shapes;
  - managed-settings paths;
  - MDM-managed Codex prefs.

  None of these is in R18's accepted scope.
- **The Codex isolation probe reads `world_state` from the rollout.** If a Codex update renames those keys, `codex_isolation` fails until the probe is updated. That's deliberate (fail closed), but it may need maintenance.
- **`claude_isolation` evidence lists account-synced skills**, accepted per R28: currently 17, including `design`, `doctor` and the `anthropic-skills:*` set.
- **Evidence is capped at 400 characters per probe** in storage and output. The full per-check list is summarised, not stored.

## Full suite

`uv run pytest`: 963 passed in 461 s (863 before this change, plus the 100 in `test_doctor.py`).
