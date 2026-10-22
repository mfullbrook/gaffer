# T4 report: scaffold and config

- **Branch:** `gaffer-v0.1-design`
- **Commit:** `7d5d530` (not pushed)
- **Files:** `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `src/gaffer/__init__.py`, `src/gaffer/config.py`, `tests/conftest.py`, `tests/test_config.py`, `prompts/.gitkeep`
- **Verify:** `uv run pytest tests/test_config.py` passes all 162 tests.
- **Scope:** T4 only. No children, store, workspace, reviews or runner code. Nothing outward-facing.

## What's there

- **Scaffold:** uv package with `src/gaffer/`, `tests/` and `prompts/` on Python 3.14. The `gaffer` entry point does nothing and exits 0. pytest is the only dependency added; parsing uses the stdlib's `tomllib`. I removed the empty README that `uv init` creates, since the README is T12's.
- **Schema:** `config.py` has every field in the T4 brief, plus `base`, the repo map keyed by Linear team key, and `round_timeout`, which the amended Modules table also lists. Defaults: test 900 s (R4), round 1800 s (R8), check-appear 600 s (R11) and CodeRabbit-appear 600 s (R27).
- **Validation:** every problem is reported in one pass, each naming the field and the fix. `ConfigError` and `KeychainError` both carry exit code 4 (R8). An empty `required_checks` is refused unless `allow_no_ci = true` (R11). Unknown keys are rejected, and a key like `github_token` is pointed at the Keychain. `allowed_mcp` rejects Linear and claude.ai connector names (R3).
- **Network allowlist:** built from ecosystems (R14, R29):
  - python: `pypi.org`, `files.pythonhosted.org`
  - node: `registry.npmjs.org`
  - php: `repo.packagist.org`, `packagist.org`, `api.github.com`, `codeload.github.com`

  `github.com` is never allowed. `extra_hosts` rejects every GitHub host, so a non-PHP repo can't add them.
- **Deny list:** the full list from R1, R20, R28 and R29, the same for every ecosystem: `~/.ssh`, `~/.config/gh`, `~/.aws`, `~/Library/Keychains`, `~/.claude`, `~/.codex`, `~/.gaffer`, `~/.claude.json`, `~/.composer`, `~/.config/composer`, `~/Library/Application Support/Herd/config/valet`, and the vault.
- **Keychain:** the token comes back as a `Secret` that prints as `<redacted>`. It can't be pickled, and Python refuses it as a value in a child's environment. Errors never repeat the token or the `security` tool's output. Tests use a fake keychain, and `tests/conftest.py` fails any test that reaches the real `/usr/bin/security`.

## Decisions that look wrong or are missing

1. **The vault has no path in the docs.** R1 and R20 deny "the vault", but nothing records where it is. I added a required global `vault_path`, which must exist, so a typo can't silently leave the vault readable. gaffer.toml is also refused if it sits inside the vault.
2. **`~/.gaffer` is on the deny list, and R9 puts job state under `~/.gaffer/jobs/`.** If job dirs go there, the sandbox can't read its own job dir. The spike used temp dirs, so it never hit this. T7 needs a job root outside `~/.gaffer`, and config doesn't define one yet. The original design's "worktree root" isn't in the amended Modules table.
3. **CI workflow files are on two lists.** The design's Writer rules make them a forbidden path (job blocked); R7 makes them a harness path (allowed with a trailer). I kept both, and forbidden is checked first, so the harness entry never applies. Separately, `.github/actions/**` (local actions that workflows call) isn't covered by either list.
4. **`allowed_mcp` is names only.** That's enough for R28's check that a child's MCP servers are on the list. But `--mcp-config` needs full server definitions, and the ledger doesn't say where they live. That's for T5 to decide.
5. **Not in the schema:** pinning the writer's model (R28 lists it as still unproven), and `lint_command` from the original design. Neither is in the amended Modules table, so I left them out.

## Choices made where the docs are silent

- The default config path is `~/.gaffer/gaffer.toml`, which means children can't read it.
- The Keychain entry is service `gaffer`, account `github-token`.
- Repo `harness_paths` and `forbidden_paths` only add to the defaults; they can't remove them. Path patterns are repo-relative globs matched with `PurePosixPath.full_match`.
- The harness defaults add lockfiles (R14) and Composer and PHPUnit config files (R29) to R7's list.
- A runner must match one of the repo's ecosystems; `generic` matches any.
- `skippable_checks` must be a subset of `required_checks`, and `allow_no_ci = true` alongside a non-empty list is an error.
- `install_command` is optional, and timeouts are whole seconds.
- I didn't check that `test_command` includes the flags R28 requires (`--continue-on-collection-errors`, `--no-watchman`). That belongs to the T8 adapters.

Any of these is a quick change. The schema needs to be settled before the other modules start in parallel.
