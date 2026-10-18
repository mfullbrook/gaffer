# Experiment 1: billing route (T1)

Run 2026-10-18, 19:57–20:03 UTC, on Mark's Mac (macOS 26.5.2), branch `gaffer-v0.1-design`. Decisions in scope: R2 (positive login-type check) and the Step 0A Pro correction.

**Question:** when headless `claude -p` and `codex exec` are launched with a stripped environment, which billing pool do they draw from, and how much of a 5-hour window does one small tester + writer + reviewer job use?

**Verdicts**

| CLI | Verdict | Basis |
|---|---|---|
| Claude Code (`claude -p`) | **subscription** (Pro) | Login is `claude.ai` / `firstParty` / `pro`; the child reports `apiKeySource: "none"`; each run emits a `rate_limit_event` against the `five_hour` subscription window with `isUsingOverage: false` |
| Codex (`codex exec`) | **subscription** (ChatGPT Plus) | `Logged in using ChatGPT`; `auth.json` `auth_mode: chatgpt` with no API key; the session rollout's `rate_limits.plan_type: "plus"` and the 300-minute `primary.used_percent` went up from 0 to 2 across the job; `credits.has_credits: false` |

No result contradicts R2, R14 or R20. R14 and R20 weren't exercised here (no sandbox network or read-denial tests); they belong to Experiment 3.

Two things are still open for Mark (see [Open items](#open-items)): the next-day billing-page re-read that R2 requires, and confirming that extra usage is off on both plans.

## 1. Versions and auth evidence

```
$ claude --version
2.1.302 (Claude Code)
$ codex --version
codex-cli 0.160.0
```

Claude Code has auto-updated since the eng review: Step 0A recorded 2.1.295. That is exactly the drift R18's auto-doctor is for. Codex is unchanged.

`claude auth status --json` (email, orgId and orgName redacted):

```json
{
  "loggedIn": true,
  "authMethod": "claude.ai",
  "apiProvider": "firstParty",
  "analyticsDisabled": false,
  "projectsDirectory": "/Users/mark/.claude/projects",
  "configDirectory": "/Users/mark/.claude",
  "email": "<redacted>",
  "orgId": "<redacted>",
  "orgName": "<redacted>",
  "subscriptionType": "pro"
}
```

```
$ codex login status
Logged in using ChatGPT
```

Both commands give the same result under the stripped environment from section 2:

```
clean-env claude auth status: {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'pro'}
clean-env codex login status: Logged in using ChatGPT
```

I also checked the config files for routes the environment strip can't remove. They were read only, and only keys were printed:

- `~/.claude/settings.json` has no `env` block keys and no `apiKeyHelper`. `~/.claude/settings.local.json` is absent, and so is `/Library/Application Support/ClaudeCode/` (managed settings).
- `~/.codex/config.toml` has no `model_provider`, `base_url` or `[model_providers]` lines.
- `~/.codex/auth.json` has the keys `OPENAI_API_KEY`, `auth_mode`, `last_refresh` and `tokens`. `auth_mode` is `chatgpt` and `OPENAI_API_KEY` is empty.

## 2. Stripped-environment proof

Every child in this experiment was started through the same launcher:

```sh
exec /usr/bin/env -i \
  HOME="$HOME" \
  PATH="/Users/mark/.local/bin:/Users/mark/.bun/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  USER="$USER" TMPDIR="$TMPDIR" LANG="$LANG" TERM="$TERM" \
  "$@"
```

The parent shell is itself a Claude Code session, and it had 14 `CLAUDE*` keys set (for example `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID` and `CLAUDE_CODE_ENTRYPOINT`). It had no `ANTHROPIC_*`, `OPENAI_*` or `*_BASE_URL` keys. Here is what the launcher passes on (keys only):

```
$ clean.sh /usr/bin/env | cut -d= -f1 | sort
HOME
LANG
PATH
TERM
TMPDIR
USER
forbidden keys in child: none
```

Each child also ran `env | cut -d= -f1 | sort` from its own shell tool during the job. Those shells see some keys the CLI adds itself on top of the six it inherits:

```
codex tester:   CODEX_CI CODEX_MANAGED_BY_BUN CODEX_MANAGED_PACKAGE_ROOT CODEX_SANDBOX CODEX_SANDBOX_NETWORK_DISABLED
                CODEX_SESSION_ID CODEX_THREAD_ID CODEX_VERSION COLORTERM GH_PAGER GIT_PAGER HOME LANG LC_ALL LC_CTYPE
                LOGNAME NO_COLOR OLDPWD PAGER PATH PWD SHLVL TERM TMPDIR USER _ __CF_USER_TEXT_ENCODING
codex reviewer: (same 27 keys)
claude writer:  _ AI_AGENT CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_EXECPATH
                CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_CODE_SESSION_ATTENDED
                CLAUDE_CODE_SESSION_ID CLAUDE_EFFORT CLAUDE_PID CLAUDECODE COREPACK_ENABLE_AUTO_PIN GIT_EDITOR HOME
                LANG LOGNAME NoDefaultCurrentDirectoryInExePath OLDPWD PATH PWD SHELL SHLVL TERM TMPDIR USER
forbidden (ANTHROPIC_*, OPENAI_*, *_BASE_URL, CLAUDE_CODE_SIMPLE): none in any of the three
```

The `CLAUDE_CODE_*` keys in the writer's shell come from the child CLI itself, not from the parent: the launcher output above shows that `env -i` removed them. `CLAUDE_CODE_SIMPLE` is absent. `--bare` was never passed.

## 3. Headless usage signals found

I probed both CLIs with a one-word prompt before relying on any signal. Below is what each one exposes, with the actual field names.

### Claude Code: `claude -p --output-format stream-json --verbose`

**Signal exists.** Each run emits one `rate_limit_event` line:

```json
{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1792371000,
 "rateLimitType": "five_hour", "overageStatus": "rejected", "overageDisabledReason": "out_of_credits",
 "isUsingOverage": false, "unifiedWindows": {"five_hour": {"utilization": 0.06, "resetsAt": 1792371000},
 "seven_day": {"utilization": 0.01, "resetsAt": 1792944000}}}, "uuid": "...", "session_id": "..."}
```

- `unifiedWindows.five_hour.utilization` and `unifiedWindows.seven_day.utilization` take values like 0.04, 0.06 and 0.07, so the resolution is 0.01. `resetsAt` 1792371000 is 2026-10-19T00:50:00Z.
- The `system`/`init` event carries `apiKeySource`, which was `"none"` on every run, plus `model` (`claude-opus-5-5`) and `claude_code_version` (`2.1.302`).
- The `result` event carries `usage` (input, cache-creation, cache-read and output tokens; `service_tier: "standard"`), `num_turns` and `total_cost_usd`. `total_cost_usd` is the CLI's own price estimate at API rates. It is not a charge, since `apiKeySource` is `none`.
- **Not in local logs:** the session transcript under `~/.claude/projects/<dir>/<session>.jsonl` has no `utilization`, `rate_limit` or `unifiedWindows` fields, and no other JSON file under `~/.claude` mentions them. The stream-json output is the only headless signal for Claude.
- **Scale is inferred, not confirmed.** I read `utilization` as a 0–1 fraction (0.06 = 6% of the 5-hour window). The evidence is that this supervising Opus session alone moved it from 0.04 to 0.07 over about 5 minutes and a few dozen tool calls, and 0.07% would be implausibly small for that. Mark's claude.ai Settings > Usage percentage would confirm it.

### Codex: `codex exec --json`

**No rate-limit fields in the `--json` stream.** The whole stream for the probe was:

```json
{"type": "thread.started", "thread_id": "01a15097-d9c9-7902-8c6f-10245e81dfe0"}
{"type": "turn.started"}
{"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "ok"}}
{"type": "turn.completed", "usage": {"input_tokens": 23821, "cached_input_tokens": 12288, "cache_write_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}}
```

**The signal is in the local session rollout instead.** `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl` gets an `event_msg` with `payload.type == "token_count"` after each model response:

```json
{"type": "token_count", "info": {"total_token_usage": {...}, "last_token_usage": {...}, "model_context_window": 475000},
 "rate_limits": {"limit_id": "codex", "limit_name": null,
   "primary":   {"used_percent": 0.0, "window_minutes": 300,   "resets_at": 1792368033},
   "secondary": {"used_percent": 0.0, "window_minutes": 10080, "resets_at": 1792954833},
   "credits": {"has_credits": false, "unlimited": false, "balance": "0"},
   "individual_limit": null, "spend_control_reached": null, "plan_type": "plus", "rate_limit_reached_type": null}}
```

- `primary` is the 5-hour window and `secondary` the weekly one. Across 1,484 `rate_limits` events in the last 14 days of rollouts, `used_percent` only ever took whole numbers (0.0, 1.0, 2.0, …), so the resolution is 1 percentage point.
- The rollout's `session_meta` records `originator: "codex_exec"`, `cli_version: "0.160.0"` and `model_provider: "openai"`. It also holds account identifiers, which aren't reproduced here.
- The `thread_id` from `--json` gives the rollout filename, so gaffer can read the signal headlessly without parsing the TUI.

## 4. The job

The job ran in a disposable repo at `/tmp/gaffer-exp1-task.*`, created with `mktemp`, outside `~/projects/gaffer` and since deleted. The ticket, DUR-1, was to implement `parse_duration("1h30m") -> 5400` with four acceptance criteria (units, whitespace and case, ordering, error cases). pytest was pre-installed into `.venv`, so no child needed network access.

| Step | CLI and flags (all via the `env -i` launcher) | Wall time | Result |
|---|---|---|---|
| probe before | `claude -p --output-format stream-json --verbose --setting-sources local --strict-mcp-config --max-turns 1`, then `codex exec --json --skip-git-repo-check --ignore-user-config --ignore-rules -s read-only` | 7 s each | "ok" |
| tester | `codex exec --json … -s workspace-write` | 27 s | wrote `tests/test_parse_duration.py`, confirmed red |
| writer | `claude -p … --max-turns 30 --permission-mode acceptEdits --allowedTools "Bash(env:*)" "Bash(.venv/bin/python -m pytest:*)"` | 17 s, 7 turns | implemented; 31 passed |
| claude probe after | as above | 5 s | "ok" |
| reviewer | `codex exec --json … -s read-only` on `git diff HEAD~1 HEAD` | 16 s | review returned |
| codex probe after | as above | 5 s | "ok" |

Final state: 3 commits (skeleton, tester tests, writer implementation) and `31 passed in 0.01s`.

## 5. Readings and deltas

### Claude (Pro): `unifiedWindows.five_hour.utilization`

| When (UTC) | Reading | Source |
|---|---|---|
| 19:58 | 0.04 | first probe |
| 20:00:19 | 0.06 | probe before writer |
| 20:01:01–20:01:18 | 0.06 | writer's own `rate_limit_event` |
| 20:01:18 | 0.06 | probe after writer |
| 20:02:55 | 0.07 | extra probe after the run |

**Writer delta: 0.00, which is below the 0.01 step.** Under the fraction reading, the writer used less than 1% of the Pro 5-hour window. It processed 6 input, 6,774 cache-write, 41,972 cache-read and 1,691 output tokens over 7 turns (`total_cost_usd` estimate $0.096). The 0.04 → 0.06 → 0.07 moves outside the writer come from this supervising session, which draws on the same Pro pool. I kept it idle while the job ran, so it isn't counted in the writer's bracket.

The 7-day window stayed at 0.01 throughout.

### Codex (Plus): `rate_limits.primary.used_percent`

| When (UTC) | Reading | Source |
|---|---|---|
| 20:00:32 | 0 | probe before |
| 20:00:39 | 1 | tester, first `token_count` |
| 20:00:59 | 1 | tester, last |
| 20:01:28 | 2 | reviewer, first |
| 20:01:37 | 2 | reviewer, last |
| 20:01:43 | 2 | probe after |

**Delta for tester + reviewer + two probes: 2 points, or 1–3% of the Plus 5-hour window given whole-number rounding.** Codex tokens:

| Run | input | cached input | output |
|---|---|---|---|
| probe before | 23,815 | 12,288 | 5 |
| tester | 126,247 | 109,568 | 922 |
| reviewer | 159,717 | 132,992 | 284 |
| probe after | 23,815 | 12,288 | 5 |

Each `codex exec` call carries a fixed overhead of about 24k input tokens even for a one-word answer. That's the size of the probe runs, and it makes each probe roughly 7% of the job's Codex input. The weekly window stayed at 0.

### What a tester + writer + reviewer job costs

| | Measured toy job | Pool |
|---|---|---|
| Claude writer | < 1% of the 5-hour window (no visible step) | Claude Pro |
| Codex tester + reviewer | about 2% (1–3%) of the 5-hour window | ChatGPT Plus |

The two CLIs bill separate pools, so each job spends a little of each window. The toy ticket finished in one pass. It used about 50k Claude tokens and about 290k Codex input tokens, and it had no repair cycles, no PR round and no CodeRabbit packet. A real job is several times larger. As a yardstick, Codex used about 1% of the Plus window per ~170k input tokens (334k across all four Codex calls for 2 points, mostly cached). Claude's cost per token can't be pinned down from this run because the writer never crossed a 0.01 step. On this evidence the Codex Plus window, not Claude Pro, is the pool more likely to bind first. Real per-job numbers should come from the v0.1 runs, as the Approach B TODO already requires.

## 6. Observations for later experiments (no decision changed)

These touch R3 and R18, not R2, R14 or R20, so they don't stop this experiment. They're recorded for Experiment 3:

1. **Claude Code drifted from 2.1.295 to 2.1.302** between the eng review and this run (R18).
2. **`codex exec --ignore-user-config --ignore-rules` still injected `~/.codex/AGENTS.md`** into the child: the rollout's `world_state.state.agents_md` holds that file's gstack text (R3).
3. **`claude -p --setting-sources local --strict-mcp-config` still loaded 3 plugins and 17 skills**, according to the `init` event (`mcp_servers: []`). Plugins and skills may need their own control, or the dedicated `CLAUDE_CONFIG_DIR` from R3 (R3).
4. Both CLIs wrote session logs for these runs under `~/.claude/projects/-private-tmp-gaffer-exp1-*` and `~/.codex/sessions/2026/10/18/`. These are session logs, not config. I left them in place as evidence.

## Open items

- [ ] **Mark: re-read the Anthropic Console and OpenAI API billing/usage pages on 2026-10-19** and confirm that no metered usage appears for 2026-10-18 around 19:57–20:03 UTC (R2 requires this before Experiment 1 is concluded).
- [ ] **Mark: confirm extra usage / overage is off** on the Claude and ChatGPT plans. Headless evidence so far: Claude reports `overageStatus: "rejected"`, `overageDisabledReason: "out_of_credits"`, `isUsingOverage: false`; Codex reports `credits.has_credits: false`, `balance: "0"`. Neither proves the setting is off rather than unfunded.
- [ ] **Mark: note the claude.ai Settings > Usage percentage** next to a fresh `utilization` reading, to confirm the 0–1 fraction reading of that field.
