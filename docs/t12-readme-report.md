# T12 report: README

- **Branch:** `t12-readme`, cut from `gaffer-v0.1-design` at `ff2a43b`. Not pushed.
- **Files:** `README.md`, the logo (`logos/svg/`, `logos/png/`, `logos/build_logo.py`), this report, and the T12 tick in `docs/designs/gaffer-overnight-pair.md`.
- **Scope:** documentation only. No code changed.

## What was checked

- **Commands and flags** against `uv run gaffer --help`, `run --help`, `doctor --help` and `eval --help`: `run <issue> [--adopt-head] [--more-cycles N] [--continue] [--fresh]`, `doctor [--config PATH]`, `eval [--config PATH] [--save-baseline | --promote RUN_ID]`. A bad argument exits 4, not argparse's 2 (`_Parser.error`).
- **Exit codes** against `runner.py` and `prompt_eval.py`. run: 0 READY, 1 internal error (a Linear failure is 1), 2 BLOCKED, 3 CLOSED, 4 refused (config, Keychain, unmapped team, preflight, doctor, locks, a live child, another active job). doctor: 0 pass, 4 failure. eval: 0, 1, 2 regression or refused `--save-baseline`, 3 incomplete, 4 refused.
- **Config example** loaded with `gaffer.config.load_config` against a temp home (a scratch script that reads the example from README.md, not committed). It parses cleanly. The defaults in the tables match `config.py`: job_root `~/gaffer-jobs`, writer_model `opus`, timeouts 900, 1800, 600 and 600 s.
- **Prerequisites** against `children.preflight` and its fix lines: the logins (`CLAUDE_CONFIG_DIR=~/.gaffer/claude claude auth login --claudeai`, `CODEX_HOME=~/.gaffer/codex codex login --device-auth`), `brew install gitleaks`, sandbox-runtime `SRT_VERSION = "0.0.79"` at `~/.gaffer/tools/sandbox-runtime` with the printed `npm install --ignore-scripts --save-exact` command, Herd's php and composer, and the clean child `PATH`.
- **Keychain:** service `gaffer`, accounts `github-token` (`config.py`) and `linear-token` (`runner.py`), with the `security add-generic-password -U -s gaffer -a <account> -w` command both error messages print.
- **Paths:** `~/.gaffer/state.db`, `~/.gaffer/jobs/<job>/report.md` and `logs/`, the lock files, `~/.gaffer/evals/`, `<job_root>/<job>/repo`.
- **Security section** against R1, R3, R14, R15, R20, R27 to R34 and the T5, T7, T8, T9, T10 reports: deny list from `DENY_READ_HOME_PATHS`, hosts from `REGISTRY_HOSTS`, env allowlist from `child_env`, Codex disabled features, the T7 git hardening.
- **Picture markup:** the `<picture>` element has a dark source (`logos/svg/gaffer-logo-dark.svg`), a light source (`logos/svg/gaffer-logo.svg`) and an `<img>` fallback with alt "gaffer" and width 400. All three relative paths exist in the worktree. The SVGs contain no script, links or external references.
- **Logo files:** only `logos/svg/`, `logos/png/` and `logos/build_logo.py` were copied from the main checkout. The drafts (`logos/*.png`, `logos/round2/`) were not.
- **Read-through against T12's Verify line:**
  - R1: residual risk is stated, including the file tools sitting outside Seatbelt and children reading whatever isn't on the deny list.
  - R3: the one-time logins are given exactly as preflight prints them. MCP servers in `allowed_mcp` are listed as outside the sandbox, and Linear is always excluded.
  - R15: `brew install gitleaks` is a prerequisite, and preflight refuses without it.
  - R16: never-sleep guidance, with the `pmset` command, plus `caffeinate` as an option for a single run.
- **Tests:** `uv run pytest`: 1086 passed in 564 s on this branch (the R34 report measured 828 s; the README says 10 to 14 minutes).

## Doc/code mismatches (not fixed; listed for Mark)

1. **`allowed_skills` does nothing.** `config.py` parses it, but nothing else reads it, and `gaffer sync-skills` (R3) was never built. R28 superseded the skills allowlist by accepting account-synced skills. The README says the field has no effect. Options: remove the field, or reject a non-empty value.
2. **Runner flags aren't validated.** `reviews.junit.missing_runner_flags` exists but neither config nor preflight calls it (T8 suggestion 1). The README lists the required flags and says gaffer doesn't check them.
3. **GitHub rate-limit headers aren't honoured,** although the design's "Waits and timeouts" asks for it (T8 item 19, T9 open issues). Listed under Known gaps.
4. **No uv cache dir for the writer** (T11 open issue 2) and **the install env is dropped** (T9 adapter debt). Neither has been fixed since. Both are listed under Known gaps.
5. **Doctor requires sandbox-runtime and node even with no `install_command`** (T10 decision 7). That is deliberate, but it isn't in the ledger. The README tells users to install both regardless.

## Open questions

1. **GitHub token permissions are inferred from the endpoints gaffer calls** (`workspace.GitHubClient`, `reviews.github`): Contents read/write, Pull requests read/write, Checks read, Commit statuses read and Actions read. They haven't been tested with a real fine-grained token, because no live `gaffer run` has happened yet. Confirm on the first real run, or switch the README to a classic `repo` token.
2. **The Linear key's permissions:** the code asks for "a read-only Linear key". I haven't checked Linear's current key-permission UI.
3. **The design's Distribution Plan** promises "a README with the kill-and-restart demo recording". That demo (SC2 to SC4) hasn't been run, so the README states the status instead. Add the recording after the toy-repo demo.
4. **Sleep guidance:** I didn't run `sudo pmset -a sleep 0` on this Mac. The System Settings wording ("Prevent automatic sleeping when the display is off", under Energy) is for current macOS desktops; laptops have it under Battery options.
5. **`--more-cycles 0` is accepted as a no-op** (it's the default), while a negative N is refused. That's harmless, so the README doesn't mention it.
