# T7 report: workspace

- **Branch:** `t7-workspace` (from `gaffer-v0.1-design`), not pushed
- **Files:** `src/gaffer/workspace.py`, `tests/test_workspace_paths.py`, `tests/test_workspace_commits.py`, `tests/test_security.py`, plus `tests/workspace_helpers.py` (shared test helpers: disposable repos, a fake gitleaks, a fake GitHub transport)
- **Verify:** `uv run pytest tests/test_workspace_*.py tests/test_security.py`. The full suite `uv run pytest` gives **338 passed, 3 skipped**. The 3 skips are the real-gitleaks tests, which run only when `gitleaks` is installed. It isn't installed here, and I didn't install it.
- **Scope:** stdlib only, no new dependencies. `config.py` is unchanged. Nothing outward-facing: the tests use tmp git repos, a local bare repo stands in for GitHub, and the HTTP layer is fake.

## What's there

- **Job clone (R21, R28, R30).** `create_workspace(config, repo, job_id, branch, token=…)` does the following:
  - Runs `git clone --local --no-checkout` from `repo.path` into `config.job_root/<job>/repo` and sets `origin` to `https://github.com/<owner/repo>.git`.
  - Writes `user.name`/`user.email` into the clone's config.
  - Fetches the base branch from origin with gaffer's token, so the job never starts from Mark's stale local branch.
  - Records `base_sha` and checks out `gaffer/<KEY>` at that SHA.

  The job dir also holds `tmp/` and, for PHP, `composer-home/cache`. `Workspace.to_record()`/`from_record()` is what the store keeps. `branch_name(key, attempt)` gives `gaffer/<KEY>` or `gaffer/<KEY>-<n>`.
- **Verification (R4, R29).** `prepare_verification(ws, repo, head, install=…)` rebuilds `<job>/verify` as a detached worktree of the job clone at exactly `head` (a full SHA is required). It checks HEAD and a clean tree, then runs the install hook. PHP installs run offline from the job's warm Composer cache; other ecosystems get the repo's registry hosts (R14).
- **Tester files (R6).** `write_tester_files` validates everything before writing anything. Rules: paths must be inside a `test_dir`, with no `..`, `.`, empty, absolute, `~`, backslash or NUL components and nothing under `.git`. No component may be a symlink. The realpath must stay inside the clone and the test dir. Targets must be regular files, not hard-linked. Limits: at most 20 files and 200 KiB each (UTF-8 bytes), with no case-insensitive duplicates. Existing tests may be edited and come back as `modified` for the report. Any violation raises `TesterOutputInvalid` (reason `tester_invalid`) listing every problem. `commit_tester_files` makes the first commit on the base and records `tester_commit` and `tester_files`.
- **Writer gates (R7, R10, R30, design Writer rules).** `check_writer_result(ws, repo, previous_head=…, cited_shas=…, nudged=…, require_new_commit=…)` returns a `GateResult`:
  - `NUDGE` with `NUDGE_PROMPT` on the first dirty tree, then `writer_dirty`.
  - `tester_commit_rewritten`.
  - `writer_no_change`, unless `require_new_commit=False` for an all-declined resume (R8).
  - `forbidden_path`, which blocks even with a trailer.
  - `tester_change_unexplained` for tester, harness or CI changes without a non-empty `Tester-Change-Reason:` trailer on that same commit. Deletes and renames count.
  - `writer_no_result` for a cited SHA that isn't a new commit in `previous_head..head`.

  `guarded_changes` lists each tester, harness and CI change with its kind and reason, for the report and the Codex reviewer.
- **Secret scan (R15).** `Gitleaks` runs `gitleaks stdin --redact --ignore-gitleaks-allow --report-format json --report-path <tmp> --exit-code 42` from an empty temp dir, with a minimal environment. Exit 42 means findings. Any other non-zero exit, or a missing or unreadable report, raises `SecretScanError` (fail closed). Findings keep only the rule, the description and a location gaffer computes, never `Secret` or `Match`.
  - `scan_push` scans every commit's message and added lines in `base..head`, so a secret that was added and then removed is still caught.
  - `scan_texts` covers comment bodies, PR text and packet items, labelled per item.
  - A hit raises `SecretDetected` (reason `secret_detected`).
- **Push.** `push(ws, head, scanner=…, expected_remote=…, token=…)` works in this order:
  - **Remote check:** runs `ls-remote` first. If the remote already has `head`, nothing happens (idempotent rerun). If the remote is anything other than `expected_remote`, it raises `PushRejected` (reason `foreign_head`).
  - **Push:** runs the gitleaks gate, then `git push <explicit URL> <sha>:refs/heads/<branch>`. There is never a `+` refspec and never `origin`. It then re-reads the remote.
  - **Token:** passed only through `GIT_CONFIG_*` env as a github.com-scoped `http.extraHeader`, never in argv, and scrubbed from errors.
- **GitHub REST.** `GitHubClient(owner/repo, Secret, transport)` runs over urllib:
  - It refuses URLs off `api.github.com`, pagination links included.
  - The real transport doesn't follow redirects, and it sends Authorization as an unredirected header.
  - Errors are `GitHubError` (never an empty result) with the token scrubbed. `repr` hides the token.
- **PR (R12, R27).** `pr_title` gives `<KEY>: <title>`. `pr_body` holds the criterion-map table, an optional untestable list and one `gaffer-job: <id>` line, and nothing more (R23). `open_pull_request` works as follows:
  - Reuses an open PR on the branch only if its marker is this job's, and otherwise raises `ForeignPullRequest`.
  - Scans the title and body before opening a new one, with `draft: false`.
  - `strip_coderabbit_notes`/`pr_job_ids` ignore text between the `release notes by coderabbit.ai` markers, and an unterminated block runs to the end of the body. This is tested against the `walkthrough_edit` fixture.
- **Round summary (R9).** `post_round_summary` lists every page of issue comments. It skips posting if a comment by `operator_login` already has the exact line `gaffer-round: <job>/<head>/<seq>`. Otherwise it scans the body and then posts it. The marker matches the `round_marker_comment` fixture.

## Trust boundary (not in the ledger, worth reading)

The job dir is the writer's write root (R21), so the clone's `.git` is writer-controlled. gaffer runs git there unsandboxed, with the token in the environment during push. Hooks, `core.fsmonitor`, clean/smudge filters, `include.path`, `url.insteadOf` and a changed `remote.origin.url` in that `.git` would all run or apply as gaffer. Mitigations, each tested by planting the attack and checking a canary:

- Every gaffer git call sets `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`. It also passes `-c core.hooksPath=/dev/null -c core.fsmonitor=false -c credential.helper= -c commit.gpgSign=false` and allows only the https and file protocols.
- `_guard` refuses a `.git` that is a symlink or a gitfile (`workspace_tampered`). If `.git/config` differs from the copy gaffer wrote, `_guard` restores gaffer's copy and records a note in `ws.config_restores` for the report.
- gitleaks reads stdin, never the clone, so a planted `.gitleaks.toml` or `.gitleaksignore` doesn't apply.

I checked that removing each of these protections makes its test fail.

## Interfaces for T5 and T9

- **Needed from T5:** an `InstallRunner`, i.e. any callable `(InstallRequest) -> InstallResult`. `InstallRequest` has `command`, `cwd`, `write_root` (the job dir), `allowed_hosts` (empty means no network) and `env` (`TMPDIR`, R29's `COMPOSER_*` and `npm_config_cache`, to set inside the wrapped command), plus `timeout` (15 min by default). T5 runs it under sandbox-runtime with the deny list and Herd on PATH.
- **Exposed for T9:**
  - Setup and verification: `create_workspace`, `Workspace.to_record`/`from_record`, `run_install(ws, repo, ws.clone, runner)` after creating the clone, and `prepare_verification`.
  - Tester and writer steps: `write_tester_files` + `commit_tester_files`, and `check_writer_result`, which returns `GateAction.OK`/`NUDGE`/`BLOCKED` plus a reason.
  - Scanning and publishing: `scan_texts` for packet items (R13, R24), `push`, `remote_head`, `open_pull_request`, `post_round_summary`.
  - Errors: each carries `.reason`, the BLOCKED code. `None` means an internal error.
- **For T8:** packet text should go through `scan_texts(..., source="packet item")`.
- **For preflight (T5/T9):** `Gitleaks.available()`. `GitHubClient.authenticated_login()` can check that the token's login equals `operator_login`.

## Decisions that look wrong or are missing

1. **R21's hardlinked objects let the writer write into Mark's `.git`.** `git clone --local` hardlinks object files (verified: same inode, `nlink 2`). The sandbox allows writes by path inside the job dir, so a writer can `chmod` and rewrite `job/repo/.git/objects/..`, which is the same inode as Mark's checkout. Git doesn't re-hash loose objects on normal reads, so this could plant content in Mark's repo. That breaks R21's "Mark's main checkout and its .git are never writable by children." I kept hardlinks per the ledger (`CLONE_HARDLINKS = True`). I recommend `--no-hardlinks`, which is a one-line change.
2. **New BLOCKED reasons the ledger doesn't name.**
   - `writer_branch_moved`: HEAD is detached or on another branch.
   - `writer_history_rewritten`: the writer rewrote the previous or pushed head (never force-push).
   - `workspace_tampered`: `.git` replaced.
   - `secret_scan_failed`: gitleaks errored. This fails closed, but it needs a reason code.
   - `foreign_pr`: an open PR on the branch without this job's marker.

   All of these need runner and report wording.
3. **Restoring `.git/config` instead of blocking.** Blocking on any change would stop every husky repo, because `npm install` sets `core.hooksPath`. Restoring means the writer's own local config changes don't persist across resumes. Please confirm this is the behaviour you want.
4. **Tester files on forbidden paths are rejected.** An example is `tests/.claude/…`. The ledger's forbidden rule covers writer commits only; I applied it to tester output too.
5. **Diff scanning covers added lines and messages only.** Context and removed lines are blanked, so a secret already on the base doesn't block a job forever. Binary files aren't scanned (`git log -p` without `--text`), the same as `gitleaks git`.
6. **Non-PHP verification installs.** R14 says install_command runs in job and verification worktrees with registry hosts, and that's what I implemented. R29's "Before: shared deps" hints that non-PHP worktrees might share deps. They shouldn't for Python: an editable install in a shared `.venv` points at the job clone's `src/`, the same trap R29 found for `vendor/`.
7. **Commit identity has no config field.** It's `gaffer <gaffer@gaffer.invalid>`, as in the spike. If you want commits linked to a GitHub account, that needs a config field, which is T4's to add.
8. **Install timeout has no config field.** It uses the default 15 min.
9. **The gitleaks flags are unverified against a real binary.** `stdin`, `--redact`, `--ignore-gitleaks-allow`, `--report-path` and `--exit-code` are from gitleaks 8.x docs as I know them. The skipped real-binary tests will confirm them once `brew install gitleaks` is done. An unknown flag fails closed (`secret_scan_failed`), never open.
10. **Merge commits by the writer.** Gates diff each commit against its first parent, so merging the base into the branch would flag base-side harness changes as unexplained. Edge case; v0.1 never rebases.
11. **The job dir is left behind on a failed create.** If `create_workspace` fails partway, the job dir remains and a retry refuses it ("already exists"). T9 should remove it or adopt it.
