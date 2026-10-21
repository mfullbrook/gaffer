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
