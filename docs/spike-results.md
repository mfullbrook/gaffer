# Experiment 3: vertical spike (T3)

Run 2026-10-20 to 2026-10-21 on Mark's Mac (macOS 26.5.2), branch `gaffer-v0.1-design`. Decisions in scope: R1, R2, R3, R4, R5, R9, R14, R18, R20, R21, R22 and Open Question 2, as R26 requires.

**CLI versions:** Claude Code `2.1.304` (Experiment 1 recorded 2.1.302, so it auto-updated again; see R18) and `codex-cli 0.160.0`. The child model was `claude-opus-5-5` for probes A1–A8 and `sonnet` for writer runs. Codex used its default model.

**Toy repos:** two disposable repos created with `mktemp` under `$TMPDIR`, outside `~/projects/gaffer`, and deleted at the end. `toy-py` uses pytest and `toy-js` uses Jest with jest-junit. Each repo carries hostile project config: `.claude/settings.json` and `settings.local.json` (SessionStart and PreToolUse hooks, an `env` block, `bypassPermissions`, `sandbox.enabled: false`), `.mcp.json`, `CLAUDE.md`, a project skill, `.codex/config.toml` (`developer_instructions`, `danger-full-access`, an MCP server), `.codex/hooks.json` and `AGENTS.md`. Every hostile item writes a canary file under `.canary/` if it runs, or plants a marker word (PINEAPPLE / BANANA / KIWI) if it loads.

Scripts are in `spike/`; evidence excerpts are in `spike/evidence/`. All probes are written so `gaffer doctor` (R18) can reuse them.

## Results

| # | Proof | Result | Ledger decisions |
|---|---|---|---|
| Login | Subscription logins in the dedicated dirs; R2 check | **PASS** | R2, R3 |
| A1 | Claude child ignores project settings, hooks, `.mcp.json`, `CLAUDE.md` and project skills | **PASS** | R3, OQ2 |
| A2 | Linear MCP and claude.ai connectors absent | **PASS**, with `--strict-mcp-config` (they load without it) | R3 |
| A3 | Only the allowlisted skill loads | **PASS, with a gap**: needs a generated denylist (see [Differences](#differences-from-the-ledger), item 1) | R3 |
| A4 | Codex ignores hostile project `.codex/config.toml` and hooks | **PASS** | R3, OQ2 |
| A5 | Codex skills and AGENTS.md controllable | **PASS** with extra flags (`CODEX_HOME` alone does not isolate skills) | R3 |
| A6 | Headless structured output, both CLIs | **PASS** | R4, R5, R10 |
| B1 | `git commit` works inside the per-job clone under the writer sandbox | **PASS** | R1, R21 |
| B2 | Writes to the source repo, its `.git/hooks`, `$HOME` and `~/.gaffer` denied | **PASS** | R1, R21 |
| B3 | Credential reads denied (Bash sandbox and file-tool Read rules) | **PASS** | R1 |
| B4 | Registry hosts reachable; api.github.com, github.com and example.com blocked | **PASS** | R14 |
| B5 | Dependency install (pip and npm) inside the writer sandbox | **PASS**, but pip needs `--use-deprecated=legacy-certs` or `uv` | R14 |
| C1 | Codex tester/reviewer children: credential reads denied | **PASS** (Codex permission profile) | R20 |
| C2 | gaffer `test_command`: credentials denied, no network, writes = job dir | **PASS** (Codex permission profile) | R20, R4 |
| C3 | gaffer `install_command`: credentials denied, registry hosts only | **PASS**, but needs the **experimental** `network_proxy` feature | R20, R14 |
| C4 | Seatbelt wrapper (`sandbox-exec`) as an alternative | Holds for plain processes; **fails** around Codex (nesting refused) | R20 |
| D1 | Codex tester returns a criterion map plus file contents, read-only | **PASS** | R5, R6, R22 |
| D2 | Valid red: assertion (pytest and Jest) | **PASS** | R5 |
| D3 | Valid red: missing `expected_missing` symbol, including a pytest collection-level error | **PASS** | R5, R22 |
| D4 | Invalid red: seeded typo (pytest and Jest) | **PASS**, but only with exception-type classification (item 5) | R5, R22 |
| D5 | Every criterion-map identity matches a JUnit testcase | **PASS**: Jest 4/4 on base; pytest 2/4 on base (collection error) and 4/4 on the writer's head | R22, R4 |
| E1 | Writer started with `--session-id`, implements and commits | **PASS** (commit needed a second turn; item 7) | R9, R21 |
| E2 | Resume by ID from a new process | **PASS** | R9 |
| E3 | Resume again from a fresh login shell; turn-one fact recalled | **PASS** | R9 |
| E4 | Session files live under `~/.gaffer/claude` | **PASS** | R3, R9 |

No proof failed outright. So, under R26, no decision reopens on a failed proof. Two results depend on controls the ledger didn't anticipate: A3's skill denylist and C3's experimental proxy. They're listed under [Differences](#differences-from-the-ledger) with options and no choice made. Whether either should reopen R3 or R14 is Mark's call.

## Login (R2, R3)

The ledger expected a dedicated config dir to need its own login, and it does. Both flows worked from Mark's phone with no terminal at his desk:

- **Codex:** `CODEX_HOME=~/.gaffer/codex codex login --device-auth` prints a URL (`https://auth.openai.com/codex/device`) and a one-time code that expires in 15 minutes. The first code expired overnight, so the login was restarted the next day.
- **Claude:** `claude auth login` has no device flow. Run headless with `BROWSER=/usr/bin/true`, `CLAUDE_CONFIG_DIR=~/.gaffer/claude claude auth login --claudeai` prints an authorize URL (`redirect_uri=https://platform.claude.com/oauth/code/callback`) and waits at `Paste code here if prompted >`. The process read the pasted `code#state` from a named pipe (`mkfifo`, held open with `exec 3<>`). Running it under `script` for a pty failed because the background job's stdin isn't a tty.

Nothing was copied from `~/.claude`, `~/.codex` or the Keychain. R2 check (`spike/r2_auth.sh`, evidence `r2_auth.txt`):

```
claude auth status --json  → loggedIn true, authMethod "claude.ai", apiProvider "firstParty",
                              subscriptionType "pro", configDirectory "~/.gaffer/claude"
                              (email/orgId/orgName redacted)
codex login status         → Logged in using ChatGPT
R2 PASS
```

**Credentials are plain files in the dedicated dirs:** `~/.gaffer/claude/.credentials.json` and `~/.gaffer/codex/auth.json`. With `CLAUDE_CONFIG_DIR` set, Claude stores the OAuth token as a file, not as a Keychain item. That's why `~/.gaffer` has to be on every deny list (item 3).

## A. Config isolation

### Claude (`spike/a_claude_init.sh`, evidence `a_claude_init.txt`)

Base command, run in the hostile toy repo:

```
env -i HOME PATH USER TMPDIR LANG TERM CLAUDE_CONFIG_DIR=~/.gaffer/claude \
claude -p "<prompt>" --output-format stream-json --verbose \
  --settings <gaffer file> --setting-sources user \
  --mcp-config '{"mcpServers":{}}' --strict-mcp-config \
  --session-id <uuid> --max-turns 1
```

| Run | Change from base | Init event / result |
|---|---|---|
| a1 | none (the flags Mark specified) | `mcp_servers: []`; 3 built-in plugins (`cc-plugin-agents-md`, `-telemetry`, `-plugin-authoring`); 17 bundled skills (`design`, `dataviz`, `loop`, `schedule`, …); 22 tools including `CronCreate`, `RemoteTrigger`, `PushNotification`, `SendMessage`, `Task`; no canaries; project `hostile-skill` absent |
| a2 | prompt asks the model to quote any CLAUDE.md/AGENTS.md text, tools off | `NONE` |
| a2c | **control:** `--setting-sources user,project` | quotes `CLAUDE.md` verbatim, ends with `PINEAPPLE-CLAUDEMD`; the `claude_hook_sessionstart` canary fires. The probe can detect both. |
| a3 | no `--mcp-config`, no `--strict-mcp-config` | **all 8 claude.ai connectors load in the dedicated dir**: Claude Docs, Instant Domain Search, Google Drive, Gmail, Google Calendar, **Linear** (`failed`), Context7, Notion, plus 24 `mcp__claude_ai_*` tools. Project `.mcp.json` still doesn't load. |
| a5 | as a3 plus `ENABLE_CLAUDEAI_MCP_SERVERS=false` | `mcp_servers: []`, no `mcp__` tools: an independent second layer |
| a4 | `code-loop` copied to `~/.gaffer/claude/skills/` (SHA-256 `c6281f66…b83b08e`); `--tools Bash,Read,Edit,Write,Skill`; env `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` | tools fixed to the 5; `code-loop` loads; but by now **account-synced content had arrived** in `~/.gaffer/claude/plugins/synced/` and `skills/synced/`: plugin `cowork-plugin-management@synced` and 14 `anthropic-skills:*` skills (docs, google-workspace, computer-use, deep-research, morning, …) |
| a6/a7 | plus `CLAUDE_CODE_SYNC_PLUGINS=0|false`, `CLAUDE_CODE_SYNC_SKILLS=0|false` | no change: already-synced content still loads |
| a8 | plus a settings fragment from `spike/gen_lockdown.sh`: `enabledPlugins` (every plugin in the init event → `false`), `skillOverrides` (every non-allowlisted skill → `"off"`), `disableBundledSkills: true` | **`tools: [Bash, Edit, Read, Skill, Write]`, `mcp_servers: []`, `plugins: []`, `skills: ["code-loop"]`** |
| a_schema | `--output-format json --json-schema <done/blocked schema>` | `structured_output: {"status":"done","summary":"probe","question":null}` with the pre-assigned `session_id` (evidence `a_claude_json_schema.txt`) |

No canary fired in any run with `--setting-sources user`. That includes the writer runs in B and E, where the Bash, Read, Edit and Write tools were used, so the hostile PreToolUse hook had chances to fire.

`init.apiKeySource` was `"none"` on every run.

### Codex (`spike/c_codex_probe.sh`, evidence `a_codex_config.txt`)

```
env -i … CODEX_HOME=~/.gaffer/codex codex exec --json -s read-only --ignore-rules \
  --output-schema spike/schemas/codex_probe.json -o <last.json> -C <toy-py> "<prompt>" < /dev/null
```

| Run | Change | Effective config (rollout `turn_context` / `world_state`) |
|---|---|---|
| c1 | base | `sandbox_policy: read-only`, `network: restricted`, approval `never`. The hostile `.codex/config.toml` is **not applied**: 0 `BANANA-CODEXCFG` matches, no `danger-full-access`, no project MCP or hook canary. **AGENTS.md is injected** (`KIWI-AGENTSMD`). **73 skills offered**, from `~/.agents/skills` (Mark's gstack set: `ship`, `land-and-deploy`, `setup-browser-cookies`, `cso`, …), `~/.gaffer/codex/skills/.system` and account-synced `~/.gaffer/codex/plugins/cache/openai-curated-remote` |
| c2 | `-c project_doc_max_bytes=0`, `--disable multi_agent apps plugins browser_use computer_use hooks image_generation in_app_browser remote_plugin goals` | AGENTS.md gone (`agents_md: {}`); 66 skills, still from `~/.agents/skills` |
| c3 | plus `-c skills.include_instructions=false` | 0 skills offered |
| c4 | c2 plus `HOME=<empty dir>` (auth still via `CODEX_HOME`) | 4 skills, only Codex's `.system` set; login works |

The project config was ignored because the toy repo isn't a trusted project in `~/.gaffer/codex/config.toml`. gaffer must never write a `[projects."…"] trust_level` entry there. **`codex exec` blocks on stdin** ("Reading additional input from stdin…") unless stdin is `/dev/null`. **Structured output:** `--output-schema` together with `-o` produced schema-valid JSON on every run. `--json` gives `thread.started.thread_id`, and that ID names the rollout file holding the rate-limit signal.

## B. Writer sandbox in a per-job clone (`spike/b_writer.sh`, `b_probe.sh`, `writer_settings.sh`)

Job dir = `mktemp -d`, then `git clone --local <toy>`, then `git remote set-url origin https://github.com/gaffer-spike/toy-<eco>.git`. The writer ran the probe battery through its Bash tool, then tried file-tool Read and Write outside the job. Generated settings, with the lockdown fragment merged in:

```json
{
  "permissions": {"deny": ["WebFetch", "WebSearch", "mcp__claude_ai_Linear",
     "Read(//Users/mark/.ssh/**)", "Read(//Users/mark/.config/gh/**)", "Read(//Users/mark/.aws/**)",
     "Read(//Users/mark/Library/Keychains/**)", "Read(//Users/mark/.claude/**)", "Read(//Users/mark/.codex/**)",
     "Read(//Users/mark/.gaffer/**)", "Read(//Users/mark/.gaffer-spike-canary/**)"]},
  "disableAllHooks": true,
  "sandbox": {"enabled": true, "failIfUnavailable": true, "autoAllowBashIfSandboxed": true,
     "allowUnsandboxedCommands": false,
     "filesystem": {"allowWrite": ["<job dir>"], "denyRead": ["~/.ssh", "~/.config/gh", "~/.aws",
        "~/Library/Keychains", "~/.claude", "~/.codex", "~/.gaffer", "~/.gaffer-spike-canary"]},
     "network": {"allowedDomains": ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]}},
  "enabledPlugins": {"…": false}, "skillOverrides": {"…": "off"}, "disableBundledSkills": true
}
```

(Paths are absolute in the real file. The `~` here is for brevity.)

Probe output (evidence `b_py2.txt`, `b_js.txt`; identical rows in both):

```
write job clone                              ALLOWED
write source repo                            DENIED
write source .git/hooks                      DENIED
write $HOME                                  DENIED
write ~/.gaffer                              DENIED
stat canary / list canary dir / ls ~/.ssh    DENIED
read <every existing credential file>        unstatable (denied; files confirmed present from outside)
read ~/.claude.json                          DENIED   (not on the list; Claude protects it itself)
GET https://pypi.org/simple/pip/             200
GET https://registry.npmjs.org/left-pad      200
GET https://files.pythonhosted.org/          404      (host reachable; root path has no page)
GET https://api.github.com/zen               000      deny network-outbound api.github.com:443
GET https://example.com/                     000      deny network-outbound example.com:443
GET https://github.com/                      000      deny network-outbound github.com:443
git ls-remote origin (fake GitHub)           DENIED
npm install (jest, jest-junit)               ok (190 packages)
pip install pytest (default truststore)      FAIL: SSLCertVerificationError('OSStatus -26276')
pip install --use-deprecated=legacy-certs    ok
uv pip install (bundled roots)               ok
git commit                                   ok dfcffca / 5defdba
```

Existing credential files, checked from outside without reading them: `~/.ssh/id_ed25519`, `~/.ssh/config`, `~/.config/gh/hosts.yml`, `~/Library/Keychains/login.keychain-db`, `~/.claude/.credentials.json`, `~/.claude.json`, `~/.codex/auth.json`, `~/.gaffer/claude/.credentials.json`, `~/.gaffer/codex/auth.json`. `~/.aws/credentials` and `~/.ssh/id_rsa` don't exist.

**File tools** (evidence `b_read_rules.txt`). In `-p` with `acceptEdits`, Read and Write outside the job dir are refused with "you haven't granted it yet". That's the missing approval prompt, not a rule. To prove the rules themselves, Read was retried under `--permission-mode bypassPermissions`:

- My first merge (jq `*`) replaced the writer's `permissions.deny` array with the lockdown fragment's. The canary was then **readable**: a spike bug, now fixed.
- With the rules present, all four syntaxes deny under bypass: `Read(//abs/**)`, `Read(~/rel/**)`, an exact file, and a bare directory. The error is `File is in a directory that is denied by your permission settings`.

**Probe caveat:** BSD `head` rejects `-c 0` ("illegal byte count"), so every read probe uses `head -c 1 > /dev/null`. That reads one byte and discards it, and no contents are ever printed. `stat` and `ls` results are unaffected.

## C. Credential denial for Codex children and gaffer's test runs (R20)

Candidates in the order Mark asked for:

1. **Codex permission profiles: hold.** `codex sandbox` with the built-in `-P :read-only` or `:workspace` profile still **reads `~/.ssh/config`**, which confirms Step 0A. A custom profile with `"<path>"="deny"` entries blocks it (`spike/codex_profiles.sh`):

   ```
   -c 'default_permissions="gaffer_ro"'
   -c 'permissions.gaffer_ro.filesystem={":root"="read","~/.ssh"="deny","~/.config/gh"="deny","~/.aws"="deny",
        "~/Library/Keychains"="deny","~/.claude"="deny","~/.claude.json"="deny","~/.codex"="deny","~/.gaffer"="deny"}'
   -c 'permissions.gaffer_ro.network.enabled=false'
   ```

   (Absolute paths in the real args.) `gaffer_test` adds `"<job dir>"="write"`. `gaffer_install` adds `--enable network_proxy`, `network.enabled=true` and `network.domains={"pypi.org"="allow","files.pythonhosted.org"="allow","registry.npmjs.org"="allow"}`. Probe results (evidence `c_profiles.txt`, no model call):

   | Probe | `ro` | `test` | `install` |
   |---|---|---|---|
   | 9 credential files, canary read + stat | DENIED | DENIED | DENIED |
   | read job / source repo file | allowed | allowed | allowed |
   | write job dir / source repo / `$HOME` | denied / denied / denied | **allowed** / denied / denied | allowed / denied / denied |
   | pypi, npm | 000 | 000 | **200** |
   | api.github.com, example.com | 000 | 000 | 000 |

   - **Codex tester/reviewer child** (evidence `c_codex_child.txt`). `codex exec` with the `gaffer_ro` args ran a script that tries each credential file. All were DENIED, and `.gitignore` was readable. In an earlier run where the paths were visible in the prompt, the model *declined* to try them; that isn't OS evidence, which is why the script hides them. The rollout's `permission_profile` lists every deny entry.
   - **install_command** (evidence `c_install.txt`): `python3 -m venv .venv && .venv/bin/pip install pytest` gives `pytest 9.1.1`, and `npm ci` gives 190 packages with jest. Both ran under `gaffer_install` in fresh job clones. pip's default truststore works here, unlike under the Claude sandbox. The proxy sets 6 proxy and CA env vars, which is the likely reason (not isolated).
   - **test_command:** the D runs below used `gaffer_test`.

2. **Seatbelt profile around the process (`sandbox-exec -f spike/seatbelt_test.sb`):** this holds for a plain process: SSH config and canary denied, no network. Wrapping the Codex CLI fails, for two reasons. The Codex process needs `CODEX_HOME` (with `auth.json`) readable. And Codex's own inner Seatbelt can't apply inside an outer one: `sandbox-exec: sandbox_apply: Operation not permitted`. A plain Seatbelt profile also has no domain allowlist, so it can't cover `install_command`.
3. **Anthropic sandbox-runtime:** not tried, because it's a new install that needs Mark's approval, and candidate 1 holds.

**Spike bug, fixed:** granting `":tmpdir"="write"` let `test` write to the *source* repo, because the toy repos lived under `$TMPDIR`. gaffer's profiles grant only the job dir, and `TMPDIR` points to `<job>/tmp`.

## D. Red-first via JUnit (`spike/d_tester.sh`, `d_redfirst.py`)

1. **Tester.** The Codex tester ran under `gaffer_ro` with `--output-schema spike/schemas/tester.json`. It returned 3 criteria and 4 tests per repo (3 acceptance, 1 guard), `expected_missing` (`toy.strings.word_count` / `wordCount`) and the contents of 2 files. The prompt fixed only the identity convention and the file split (evidence `d_tester_*.json`).
2. **Writing the files.** The spike script wrote them into a detached verification worktree at the base SHA. Paths had to be relative, contain no `..`, start in `test_dirs` and cross no symlinks (R6).
3. **Running tests.** Tests ran under the Codex `gaffer_test` profile with JUnit output:
   - pytest: `.venv/bin/python -m pytest -q -p no:cacheprovider -o junit_family=xunit2 --continue-on-collection-errors --junitxml=<job>/junit.xml`
   - Jest: `jest --ci --no-watchman --reporters=default --reporters=jest-junit` with `JEST_JUNIT_CLASSNAME={filepath}`, `JEST_JUNIT_TITLE="{classname} {title}"`, `JEST_JUNIT_ADD_FILE_ATTRIBUTE=true`, `JEST_JUNIT_REPORT_TEST_SUITE_ERRORS=true`

Classification:

| Variant | Test | JUnit | Verdict |
|---|---|---|---|
| py clean | `test_word_count` ×2 | none; file-level `error` (classname `""`, name `tests.test_word_count`) | **VALID red: collection-level ImportError names `word_count`** |
| py clean | `test_shout_strips_surrounding_whitespace` | failure | **VALID red: assertion** |
| py clean | `test_shout_uppercases_inner_text` (guard) | passed | guard ok |
| py typo (`shout(`→`shuot(` in one acceptance test) | `test_shout_strips_…` | **failure** | **INVALID red: NameError not naming expected_missing** |
| js clean | `wordCount …` ×2 | failure: `TypeError: wordCount is not a function` | **VALID red: names `wordCount`** |
| js clean | `shout trims …` | failure: `expect(received).toBe` | **VALID red: assertion** |
| js typo | `shout trims …` | **failure**: `ReferenceError: shuot is not defined` | **INVALID red** |
| js suite (missing `require` seeded) | both `shout.test.js` tests | none; suite testcases (`error` + `failure`, classname `Test suite failed to run`) | INVALID red: `Cannot find module` |

Identity coverage:

- Jest: 4/4 identities matched a JUnit testcase on base.
- pytest on base: 2/4, because the collection error yields one file-level testcase instead of the two tests.
- pytest on the writer's head `4db1287`, run under `gaffer_test`: **4/4 matched and all passed** (evidence `e_verify_py.txt`).

## E. Resume by pre-assigned ID (`spike/e_writer.sh`, evidence `e_resume.txt`)

Writer flag set (below), session `e6373801-…`, run in the py job clone with tester tests committed on `gaffer/TOY-1`:

1. **Turn 1 (`--session-id`).** The writer got the fact "codename HELIOTROPE-47" plus the task, implemented it, and reached `5 passed`. Both of its commits were **held for approval** ("This command requires approval"). The commands were `git -c user.name=… -c user.email=… commit …`, and Claude Code doesn't auto-allow `git -c` in the sandbox. The process exited with a dirty tree.
2. **Resume 1** (`--resume`, new process). gaffer had set `user.name` and `user.email` in the job clone's `.git/config` in the meantime. A plain `git commit -am` gave `DONE 4db1287`, clean tree, same `session_id`.
3. **Resume 2** (`--resume` from `env -i … /bin/zsh -l -c`, a fresh login shell, pid 66354). Without tools, the writer answered: "(a) HELIOTROPE-47 (b) `toy/strings.py` (c) `4db1287`".
4. **Session files.** The transcript is `~/.gaffer/claude/projects/-private-var-folders-…-gaffer-spike-job2-py-…-repo/e6373801-….jsonl`, and there's also `~/.gaffer/claude/session-env/<id>/`. Nothing was written under `~/.claude/projects`. The directory name is derived from the cwd, so a resume has to run from the same job clone path.

## Final working flag sets

Common launcher: `env -i HOME PATH USER TMPDIR LANG TERM` plus `CLAUDE_CONFIG_DIR=~/.gaffer/claude` and `CODEX_HOME=~/.gaffer/codex`, with stdin `/dev/null`. Never `--bare`, never an API key.

**Claude writer** (`spike/e_writer.sh`, `writer_settings.sh`, `gen_lockdown.sh`):

```
env … ENABLE_CLAUDEAI_MCP_SERVERS=false CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1 CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 \
      BASH_DEFAULT_TIMEOUT_MS=600000 BASH_MAX_TIMEOUT_MS=600000 \
claude -p "<prompt>" --output-format stream-json --verbose [--json-schema <result schema>] \
  --settings <generated: R1/R14 sandbox + Read denies + disableAllHooks + enabledPlugins/skillOverrides lockdown> \
  --setting-sources user --mcp-config '{"mcpServers":{}}' --strict-mcp-config \
  --tools "Bash,Read,Edit,Write,Skill" --permission-mode acceptEdits \
  --session-id <uuid>        # first launch
  --resume <uuid>            # every later turn, from the same job clone cwd
```

Workspace setup before launch: `git clone --local`, `origin` set to the GitHub URL, `user.name` and `user.email` set in the clone's config, and `TMPDIR` for children set inside the job dir. Writer instructions must say to install Python deps with `uv` or `pip --use-deprecated=legacy-certs`, and to pass `--cache <job>/.npm-cache` to npm.

**Codex tester / reviewer** (`spike/codex_child_args.sh`):

```
env … HOME=<job>/tmp/home \
codex exec --json --ignore-rules \
  -c 'default_permissions="gaffer_ro"' -c 'permissions.gaffer_ro.filesystem={":root"="read", <credential paths>="deny"}' \
  -c 'permissions.gaffer_ro.network.enabled=false' \
  -c project_doc_max_bytes=0 -c skills.include_instructions=false -c 'web_search="disabled"' \
  --disable multi_agent --disable apps --disable plugins --disable browser_use --disable computer_use \
  --disable hooks --disable image_generation --disable in_app_browser --disable remote_plugin --disable goals \
  --output-schema <schema> -o <result.json> -C <job clone> "<prompt>" < /dev/null
```

**gaffer test_command:** `env … TMPDIR=<job>/tmp codex sandbox <gaffer_test -c args> -P gaffer_test -C <verify worktree> -- <test_command>`. pytest needs `--continue-on-collection-errors` and `-o junit_family=xunit2`. Jest needs `--ci --no-watchman` plus the jest-junit env above.

**gaffer install_command:** `codex sandbox --enable network_proxy <gaffer_install -c args> -P gaffer_install -C <checkout> -- <install_command>`.

## Differences from the ledger

Each item names the decision it touches. Where there's a choice to make, options are listed and none is chosen.

1. **R3: allowlisted skills need a generated denylist.** `--setting-sources user` doesn't stop three kinds of content:
   - **Account-synced content.** After login, Claude syncs the account's plugins and skills into the dedicated dir (`plugins/synced/`, `skills/synced/`), and they load. `CLAUDE_CODE_SYNC_PLUGINS/SKILLS=0` doesn't unload content that has already synced.
   - **Bundled skills.**
   - **Built-in plugins.**

   The working control is `enabledPlugins: false` and `skillOverrides: "off"`, generated from an init event, plus `disableBundledSkills`. That's a denylist, so a skill that syncs between the probe and the launch would load. Options:
   - (a) assert on every child's init event (`skills ⊆ allowed_skills`, `plugins = []`, `mcp_servers ⊆ allowed_mcp`) and kill the child before it acts if the check fails;
   - (b) regenerate the lockdown from an init probe right before each launch;
   - (c) `--disable-slash-commands`, which drops *all* skills, the allowlisted ones included;
   - (d) look for a setting that turns account sync off for the dedicated dir.
2. **R3: Codex's `CODEX_HOME` doesn't isolate skills or AGENTS.md.**
   - The child sees `~/.agents/skills` (Mark's gstack skills) and account-synced remote plugins. It's also offered `multi_agent` collaboration tools, `apps`, `browser_use` and `computer_use`.
   - The working controls are the flags in the Codex flag set above, including `project_doc_max_bytes=0` for AGENTS.md.
   - R3 assumed `CODEX_HOME` would carry the Codex allowlist. If Codex children should get allowlisted skills, they'd go in `<HOME override>/.agents/skills` or `$CODEX_HOME/skills` with `include_instructions` left on. Untested.
3. **R1 and R20: the deny list must include `~/.gaffer` and `~/.claude.json`.** Both dedicated dirs hold subscription credentials as plain files. The ledger lists `~/.claude` and `~/.codex` only. The Claude sandbox denied `~/.claude.json` on its own, but the Codex profile needs it listed.
4. **R1 and R20: Codex's built-in sandboxes are replaced.** R1's "Codex tester/reviewer use `-s read-only`" and "test_command runs under `codex sandbox`" both read credentials (Step 0A, reconfirmed). What holds is named permission profiles with `deny` entries, as above.
5. **R14: `install_command` depends on an experimental Codex feature.** Codex's domain allowlist only works with `--enable network_proxy` (`codex features list`: `network_proxy experimental false`). Options:
   - (a) accept it, with R18 doctor probing the allowlist on every version change;
   - (b) run `install_command` through the Claude sandbox (whose `allowedDomains` works, B4), which means a Claude child or a non-model wrapper;
   - (c) Anthropic sandbox-runtime (needs install approval);
   - (d) R14's "network stays closed" option.

   Separately, under the writer's Claude sandbox, pip's default truststore fails TLS. `uv`, or `pip --use-deprecated=legacy-certs`, works.
6. **R5: "a JUnit failure is a valid red" is not enough.** pytest reports `NameError` raised in a test body as `<failure>`. Jest reports `ReferenceError` and `TypeError` as `<failure>` too. Under R5's literal wording, the seeded typos would have counted as valid reds. The spike classifies on the exception type instead: an assertion failure is valid; any other exception is valid only if its text names an `expected_missing` symbol. That matches R5's intent and the design's failure-mode row ("Acceptance test fails on a typo").
7. **R22: adapter facts.**
   - **pytest.** It aborts the whole run on a collection error unless `--continue-on-collection-errors` is passed. xunit2 has no `file` attribute: map `classname` to the file instead. A collection error is a single testcase with classname `""` and the module path as its name.
   - **Jest.** Under the sandbox, watchman fails and Jest **exits 0 having run no tests**. So it needs `--no-watchman`, and an empty or missing JUnit file must count as failure (R4: never trust exit codes). jest-junit reports a suite failure as two testcases (`error` + `failure`) with classname `Test suite failed to run` and the file path as the name.
   - Vitest was not tested.
8. **R21: commit identity.** The writer can't use `git -c` (it needs approval). gaffer should write `user.name` and `user.email` into the job clone's config.
9. **R9: resume is tied to the cwd.** Sessions are stored under a directory derived from the cwd, so every resume must run from the job clone path. Resuming from another directory was not tested.
10. **R18: version drift.** Claude Code went from 2.1.295 to 2.1.302 to 2.1.304 between the review and this spike.
11. **Open, not proven:**
    - Codex `web_search="disabled"` was set but not probed. Codex's web search runs on OpenAI's side, outside the local sandbox, so this is still unproven.
    - The writer's model choice isn't fixed by any flag above (the spike used `sonnet`).

## Quota used

Signals as found in Experiment 1. Claude: `rate_limit_event.unifiedWindows` in stream-json. Codex: rollout `token_count.rate_limits`.

| Pool | Start of proofs | End | Notes |
|---|---|---|---|
| Claude Pro, 5-hour | 0.23 | 0.39 | Shared with this supervising Opus session, which ran throughout. The 18 child runs used about 301k input tokens (mostly cache reads) and 6.5k output tokens. |
| Claude Pro, 7-day | 0.08 | 0.10 | |
| Codex Plus, 5-hour | 0 | 3 | 8 `codex exec` runs (4 config probes, 2 children, 2 testers). Every `codex sandbox` probe and test run is free. |
| Codex Plus, weekly | 0 | 1 | |

`isUsingOverage` was `false` on every Claude reading.

## State left behind

- **Kept:** `~/.gaffer/claude` and `~/.gaffer/codex` (logins, the copied `code-loop` skill, account-synced content and session logs from the spike).
- **Deleted:** the toy repos, the job clones and `~/.gaffer-spike-canary*`.
- **Unchanged:** `~/.claude` and `~/.codex`.

## 3b: PHP

Run 2026-10-22, branch `gaffer-v0.1-design`, for R28's "PHP installs must be proven before module code". Decisions in scope: R14, R20 and R22.

**Versions:**

- Claude Code `2.1.304`, `codex-cli 0.160.0`.
- Laravel Herd's PHP `8.4.22` and Composer `2.10.1`, from `~/Library/Application Support/Herd/bin`.
- **sandbox-runtime `0.0.79`** (`@anthropic-ai/sandbox-runtime`, published 2026-10-07, so at least two weeks old; the latest is 0.0.81). Installed with `npm install --ignore-scripts --save-exact` into `~/.gaffer/tools/sandbox-runtime`, not globally. Its dependencies resolved to `zod 3.25.76`, `commander 12.1.0`, `node-forge 1.4.0` and `@pondwader/socks5-server 1.0.10`. The binary is `~/.gaffer/tools/sandbox-runtime/node_modules/.bin/srt`.

**Toy repo** (`spike/make_php_toy.sh`): a disposable repo from `mktemp` that requires `monolog/monolog ^3` and `symfony/polyfill-ctype ^1.30`, with `phpunit/phpunit ^11.5` in require-dev. That came to 30 locked packages: monolog 3.12.1, polyfill-ctype 1.37.0 and PHPUnit 11.5.57 with its dependencies. `composer.lock` was resolved under sandbox-runtime with the two Packagist hosts only (`p_lock.txt`). Every job clone gets `COMPOSER_HOME=<job>/composer-home` and `COMPOSER_CACHE_DIR=<job>/composer-home/cache`.

**Model runs:** two Sonnet writer runs, both for step 3a. Everything else ran under `srt` or `codex sandbox`, which cost no quota.

### Results

| # | Proof | Ledger | Result | Evidence |
|---|---|---|---|---|
| 3b.1 | Herd php/composer run under the writer's Claude sandbox and under sandbox-runtime | R14 | **PASS**, with Herd's bin dir on the launcher's PATH | `p_herd.txt`, `p_writer_a.txt` |
| 3b.2 | `composer install` with allowedDomains = `repo.packagist.org`, `packagist.org` only: (a) writer sandbox, (b) sandbox-runtime | R14 | **FAIL** in both setups (exit 100): every dist archive comes from `api.github.com` | `p_writer_a.txt`, `p_install_trials.txt` t1 |
| 3b.3 | Smallest host set that installs | R14 | **PASS with `api.github.com` + `codeload.github.com`**: GitHub hosts are needed (options below) | `p_install_trials.txt` t3–t7, `p_writer_b.txt` |
| 3b.4 | Install with **no network** from a pre-filled Composer cache | R14 | **PASS** (30 packages, 0 hosts) | `p_install_trials.txt` t8, `p_phpunit.txt` |
| 3b.5 | R20 credential denies, write denies and blocked hosts under the candidate settings: (a) writer sandbox, (b) sandbox-runtime | R1, R14, R20 | **PASS** in both (rows identical) | `p_writer_b.txt`, `p_srt_final.txt` |
| 3b.6 | PHPUnit with JUnit under the Codex `gaffer_test` profile (network off) | R20, R22 | **PASS**: shapes recorded; a file-level fatal leaves no JUnit file | `p_phpunit.txt` |

R14's accepted scope, "php: repo.packagist.org, packagist.org … no GitHub hosts", **does not install PHP dependencies**. The ledger is unchanged; the options are below for Mark.

### Step 2: what Herd's php and composer need

- **PATH:** `php` is a real Mach-O binary (`php -> php84`), not a shim. `composer` is a phar with `#!/usr/bin/env php`. Neither is on the `env -i` PATH, so the launcher must prepend `~/Library/Application Support/Herd/bin` (the path contains a space). It must do this for the writer's `claude` process, `srt` and `codex sandbox`. The writer's Bash tool inherits the launcher's PATH as is, with Herd first (`p_writer_a.txt`).
- **ini:** Herd's own `php.ini` is found only through `HERD_PHP_84_INI_SCAN_DIR`, which Mark's `~/.zshrc` exports. Under `env -i`, PHP runs with **no ini at all**, so the Herd extension, `auto_prepend_file` (Herd's dump loader) and `openssl.cafile` are all absent. Composer then verifies TLS with its bundled CA roots, which worked in every setup. With the variable set, PHP also ran fine under the sandbox: the Herd extension and the dump loader load, and Composer works. gaffer can leave the variable unset.
- **Reads:** the Herd bin dir, `/Applications/Herd.app/Contents/Resources` (only with the ini) and the job dir. All are allowed by default.
- **Writes:** only `COMPOSER_HOME`, `COMPOSER_CACHE_DIR`, `vendor/` and `TMPDIR`, all inside the job dir. Writes to `~/.composer` were DENIED and nothing needed them.
- **Sockets:** none needed. With no ini, PHP never touches Herd's sockets (`herd.sock`, `herd84.sock`, `herd-debug.sock`), and with the ini, nothing tried to connect to them. A direct `stream_socket_client` to `herd.sock` (a symlink to the FPM socket `herd84.sock`) was refused: the sandbox log shows `deny network-outbound …/Herd/herd84.sock`, because Unix sockets are blocked by default. The macOS sandbox log for `php84` showed only `mach-lookup com.apple.diagnosticd`, `com.apple.SystemConfiguration.configd` and `sysctl-read kern.iossupportversion` denials (logging and system proxy lookup). None broke anything.
- **Sandbox quirks found on the way:**
  - **`srt` needs a short `TMPDIR`.** It binds a Unix socket in `$TMPDIR`, and `<job>/tmp` exceeded macOS's 104-byte socket path limit (`listen EINVAL`). `srt` keeps the system `TMPDIR`, and the wrapped command gets `TMPDIR=<job>/tmp` (`spike/p_srt_run.sh`).
  - **`/dev/fd` is denied under `srt`,** so bash process substitution (`>(tee …)`) fails there.
  - **Claude's Bash tool overrides `TMPDIR`** to `/tmp/claude-501`, whatever the launcher sets.

### Steps 3 and 4: hosts Composer tries

Trials ran under sandbox-runtime in a fresh job clone with an empty cache each time (`spike/p_install_trial.sh`, `-vvv`). The host lists come from Composer's own log and `srt --debug` decisions.

| Trial | allowedDomains | Result | What Composer tried |
|---|---|---|---|
| t1 | repo.packagist.org, packagist.org | **exit 100**, 0 installed | 30 dist URLs `api.github.com/repos/<o>/<r>/zipball/<sha>`, 120 denied attempts (4 tries each). Writer sandbox: identical (`p_writer_a.txt`) |
| t2 | same, `--prefer-source` | **exit 100** | `git clone https://github.com/...` ×30, denied, then the dist fallback to `api.github.com`, denied |
| t3 | + codeload.github.com | **exit 100** | Composer never rewrites to codeload itself, so the `api.github.com` requests are still denied |
| t5 | + api.github.com (no codeload) | **exit 100** | `api.github.com` answers 302 → `codeload.github.com/<o>/<r>/legacy.zip/<sha>`, denied |
| t4 | packagist ×2 + api.github.com + codeload.github.com | **exit 0**, 30 installed | the above, plus `packagist.org/downloads` (install stats) |
| t6 | repo.packagist.org + api + codeload | **exit 0** | `packagist.org/downloads` denied, ignored |
| t7 | api.github.com + codeload.github.com only | **exit 0** | `repo.packagist.org` (advisory lists) and `packagist.org/downloads` denied, ignored |
| t8 | **none**, cache pre-filled from t4 | **exit 0**, 30 from cache | Packagist calls denied, ignored |

- **Minimal set for `composer install` from a lockfile: `api.github.com` + `codeload.github.com`.** The Packagist hosts are optional for installs. `objects.githubusercontent.com` was never contacted.
- **Changing dependencies** (`composer require` or `update`) also needs `repo.packagist.org` for metadata. The lock resolve used `repo.packagist.org` only (`p_lock.txt`).
- **`--prefer-dist` vs source.** `--prefer-dist` is the default for tagged releases. `--prefer-source` needs git over `github.com` (443, and 22 via `GIT_SSH_COMMAND`), which both sandboxes deny. When `api.github.com` is allowed, Composer falls back to the dist archive and still exits 0. Source installs are therefore never needed, and `github.com` can stay blocked.
- **GitHub API rate limit.** One unauthenticated `api.github.com` request per package (30 here). The zipball endpoint answers with a 302 that GitHub tags `x-ratelimit-resource: core` but does not count: `used` stayed at 0/60 after two full installs. That was checked with `GET /rate_limit` before and after, and on one zipball request by hand. Even so, a large project (Laravel apps lock 100+ packages) would hit the 60/hour limit if these requests ever started counting. A warm cache makes repeat installs cost 0 requests.

### Step 5: probes under the candidate settings

The writer (Claude sandbox) and sandbox-runtime gave identical rows (`p_writer_b.txt`, `p_srt_final.txt`):

```
write job dir / source repo / $HOME / ~/.gaffer / ~/.composer     ALLOWED / DENIED / DENIED / DENIED / DENIED
read + stat canary                                                DENIED
read ~/.ssh/{id_ed25519,config}, ~/.config/gh/hosts.yml,
     ~/Library/Keychains/login.keychain-db, ~/.claude/.credentials.json,
     ~/.claude.json, ~/.codex/auth.json, ~/.gaffer/{claude,codex} creds   DENIED
read ~/.composer/latest.phar                                      DENIED
read Herd Valet CA private key                                    DENIED
read job file / Herd php.ini                                      ALLOWED
GET repo.packagist.org 200, api.github.com/zen 200, codeload.github.com 301
GET example.com, github.com, objects.githubusercontent.com, pypi.org   000 (blocked)
git ls-remote origin (fake GitHub) / github.com/Seldaek/monolog   DENIED
composer install --prefer-source                                  github.com clones denied (https and :22); dist fallback, exit 0
```

`api.github.com/zen` returning 200 shows that allowing the host allows **every path and method on it**, not just zipballs. Neither sandbox filters by path in this configuration.

### Step 6: PHPUnit under the network-off test sandbox

PHPUnit ran under `codex sandbox -P gaffer_test` (the R28 profile: credentials denied, writes limited to the job dir, no network) as `php vendor/bin/phpunit --log-junit <job>/junit.xml --do-not-cache-result`. Herd PHP under that profile could not read the canary or `~/.ssh/config`, could not reach Packagist, and could not write `$HOME` (`p_phpunit.txt`). Before that, the verification worktree installed its dependencies under `srt` **with no hosts at all**, from the job's warm cache.

**JUnit shape** (`<testcase name file line class classname assertions time>`; `classname` uses dots, `class` the real FQCN; `file` is present, unlike pytest's xunit2):

| Case | JUnit |
|---|---|
| assertion failure | `<failure type="PHPUnit\Framework\ExpectationFailedException">` "Failed asserting that two strings are identical." |
| runtime exception | `<error type="RuntimeException">` "RuntimeException: boom" |
| TypeError | `<error type="TypeError">` |
| **missing class** | `<error type="Error">` `Error: Class "Toy\WordCounter" not found` |
| missing function | `<error type="Error">` `Error: Call to undefined function Toy\word_count()` |
| typo (undefined method) | `<error type="Error">` `Error: Call to undefined method Toy\Strings::shuot()` |
| skipped and **incomplete** | both `<skipped/>`, with no message (R7: counts as failed) |
| **file-level fatal** (missing base class, parse error) | **no JUnit file at all**, exit 255. PHPUnit aborts while building the suite, so every other test file is lost too. stdout has `Message:  Class "Toy\Testing\WordCase" not found` / `Location: <file>:<line>` |

Element text starts with `<Class>::<method>` on its own line, followed by the exception line.

What this means for the R22 adapter and R5 classification:

- **Classification:** R28's exception-type rule carries over directly. `ExpectationFailedException` is the assertion type. `Error`, `TypeError` and the rest are valid only if the text names an `expected_missing` symbol. The missing class and missing function name it; the typo doesn't.
- **File-level fatals:** "missing JUnit = failed" (R4/R7) would turn a single missing base class into a failure of the whole run. Running PHPUnit once per test file isolates it: with the broken file present, `phpunit tests/ShapesTest.php` still produced all 9 testcases. The broken file's own run then exits 255 with the `Message`/`Location` lines, which the adapter would have to parse to treat it like pytest's collection error.
- **Verification worktrees:** a **symlinked `vendor/` is wrong for PHP.** Composer's autoload maps resolve `src/` relative to vendor's real path, so the tests would load the job clone's code. Each worktree needs its own `composer install`, which works offline from the job's cache.

### Exact settings used

**Writer (Claude sandbox)**, generated by `spike/writer_settings.sh` with `ALLOWED_DOMAINS`, launched by `spike/p_writer.sh`:

```json
"sandbox": {"enabled": true, "failIfUnavailable": true, "autoAllowBashIfSandboxed": true, "allowUnsandboxedCommands": false,
  "filesystem": {"allowWrite": ["<job>"], "denyRead": ["~/.ssh", "~/.config/gh", "~/.aws", "~/Library/Keychains", "~/.claude",
     "~/.codex", "~/.gaffer", "~/.claude.json", "~/.composer", "~/.config/composer",
     "~/Library/Application Support/Herd/config/valet", "<canary dir>"]},
  "network": {"allowedDomains": ["repo.packagist.org", "packagist.org"]}}          // 3b.2 (fails)
  "network": {"allowedDomains": ["repo.packagist.org", "packagist.org",
                                 "api.github.com", "codeload.github.com"]}}        // 3b.3/3b.5 candidate (works)
```

Launch env additions: `PATH=<Herd bin>:<clean PATH>`, `COMPOSER_HOME=<job>/composer-home`, `COMPOSER_CACHE_DIR=<job>/composer-home/cache` and `COMPOSER_NO_INTERACTION=1`. The other flags are as in "Final working flag sets".

**install_command (sandbox-runtime)**, `spike/p_srt_settings.sh` and `p_srt_run.sh`:

```json
{"network": {"allowedDomains": ["<hosts>"], "deniedDomains": []},
 "filesystem": {"denyRead": ["<same deny list>"], "allowWrite": ["<job>"], "denyWrite": []}}
```

```
env -i HOME PATH=<Herd bin>:<clean PATH> USER TMPDIR=<system tmp> LANG TERM=dumb \
  COMPOSER_HOME=<job>/composer-home COMPOSER_CACHE_DIR=<job>/composer-home/cache COMPOSER_NO_INTERACTION=1 \
  ~/.gaffer/tools/sandbox-runtime/node_modules/.bin/srt --settings <file> \
  -c 'export TMPDIR=<job>/tmp; cd <checkout> && composer install --no-progress'
```

Unix sockets stay blocked (the srt default); nothing needed them.

### Options for R14 (GitHub hosts), not chosen

R14 says no GitHub hosts, and PHP installs can't work without them unless gaffer fetches the archives some other way:

- **(a) Allow `api.github.com` + `codeload.github.com` for PHP repos only** (writer and install_command). It's simple and proven here. The cost: the sandbox can then reach the **whole GitHub REST API**, every path and method. Code inside the sandbox (an install script, or a prompt-injected writer) could send data out by calling the API with a token of its own, for example by opening an issue on an attacker's repo. `codeload` is GET-only, so it's a much smaller channel.
- **(b) Same hosts with path filtering.** sandbox-runtime has an experimental `network.tlsTerminate` + `filterRequest` that could allow only `GET /repos/*/zipball/*` on `api.github.com`. **Untested.** The library API is needed for `filterRequest`, and the writer's Claude sandbox exposes no equivalent. So this would cover install_command only, and is experimental.
- **(c) gaffer pre-fills the Composer cache outside the sandbox.** gaffer's own downloader would fetch only the dist URLs listed in `composer.lock` (`api.github.com/.../zipball/<sha>` → codeload) into the job's `COMPOSER_CACHE_DIR`. It would check each archive against the lock reference, then run `composer install` with **no network** (t8 proves the install part). No sandboxed process gets a GitHub host. But gaffer itself then contacts GitHub, and a writer that needs a **new** PHP dependency can't fetch it, so the job ends `BLOCKED: needs_dependency` (R14 option B behavior, PHP only).
- **(d) Mixed:** (c) for install_command, plus (a) in the writer sandbox only when a ticket needs to change PHP dependencies (per repo, through `extra_hosts`).
- **(e) A dist mirror** (Private Packagist, or a self-hosted Satis/Velocita with `dist` mirroring), so archives come from a non-GitHub host. That's new infrastructure, outside v0.1 scope.

### Differences from the ledger (3b)

1. **R14:** the PHP host list in R14 can't install anything. Options above.
2. **R1 and R20 deny list:**
   - **Composer credential locations.** `~/.composer` and `~/.config/composer` hold `auth.json` when Composer has GitHub, Packagist or Bitbucket tokens. Neither file exists today, but both dirs exist.
   - **Herd's local CA private key** (`~/Library/Application Support/Herd/config/valet/CA/LaravelValetCASelfSigned.key`). That CA is trusted by macOS, so whoever reads the key can mint certificates the machine trusts.

   3b denied all three, and Herd still worked.
3. **R14 install_command env:** Herd's bin dir must go on PATH. `srt` needs the short system `TMPDIR`, with the job `TMPDIR` set inside the wrapped command. Claude's Bash tool sets its own `TMPDIR`.
4. **R22 PHPUnit adapter:**
   - **Fatals:** a file-level fatal leaves no JUnit file. That needs per-file runs or stdout parsing.
   - **Shapes:** incomplete tests are `<skipped/>`. Testcases carry `file`, `class` and a dotted `classname`.
   - **Worktrees:** verification worktrees need their own `composer install`; a symlinked `vendor/` won't work.
5. **R5:** no change. The exception-type rule fits PHPUnit: `ExpectationFailedException` is the assertion type, and `Error` messages name the missing symbol.
6. **File-tool Read rules for single files.** `writer_settings.sh` builds `Read(//<path>/**)`, which doesn't match a file like `~/.claude.json`. The Bash sandbox's `denyRead` covers the file at OS level (DENIED above), but file-tool Read of single files needs exact-path rules, as Proof B showed. File-tool reads were not re-probed in 3b.

### Quota (3b)

| Pool | Before | After | Notes |
|---|---|---|---|
| Claude Pro, 5-hour | not read | 0.09 after both runs | Two Sonnet writer runs, 2 turns each. A new 5-hour window, shared with this supervising session. |
| Claude Pro, 7-day | not read | 0.12 after both runs | `isUsingOverage: false` |
| Codex Plus | unchanged | unchanged | No `codex exec`. `codex sandbox` runs are free. |
| GitHub unauthenticated core | 0/60 | 0/60 | zipball 302s were not counted |

### State left behind (3b)

- **Kept:**
  - `~/.gaffer/tools/sandbox-runtime` (sandbox-runtime 0.0.79, pinned in its `package.json` and `package-lock.json`);
  - `~/.gaffer/claude` and `~/.gaffer/codex`.
- **Deleted:** the PHP toy repo, every job clone and verify worktree, and `~/.gaffer-spike-canary`.
- **Unchanged:** `~/.claude`, `~/.codex`, `~/.composer`, `~/.config/composer` and Herd's config.
