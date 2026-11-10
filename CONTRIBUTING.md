# Contributing to gaffer

Thanks for your interest. gaffer is a small v0.1 project with one maintainer, so issues and focused pull requests are the most useful contributions.

## Before you start

- For anything bigger than a bug fix, open an issue first so we can agree on the approach.
- Read [`docs/designs/gaffer-overnight-pair.md`](docs/designs/gaffer-overnight-pair.md). It is the source of truth for how gaffer behaves. Its decision ledger (R1 to R34) records every behaviour choice and why it was made; where two records conflict, the later one wins. A change that contradicts a ledger record needs a new record, not just code.
- Security-sensitive changes (sandbox settings, deny lists, the environment allowlist, network allowlists, token handling, the secret scan) get extra scrutiny. Explain what the change allows that was blocked before.

## Development setup

You need macOS, Python 3.14 and [uv](https://docs.astral.sh/uv/). gitleaks is optional for development; tests that need the real binary are skipped without it.

```sh
git clone https://github.com/mfullbrook/gaffer.git
cd gaffer
uv sync
uv run pytest
```

The suite needs no logins, tokens or network access. It runs on fakes, recorded CLI transcripts, captured CodeRabbit fixtures and disposable git repos, and takes 10 to 15 minutes. Run a single file while iterating, for example `uv run pytest tests/test_config.py`.

## Code style

- Standard library only at runtime; pytest is the only dev dependency. Adding a dependency needs a good reason.
- Match the surrounding code: small functions, type hints, and comments that say why, not what.
- Every behaviour change comes with tests. For checks that guard isolation or correctness, break the code once and confirm the test fails before you submit (a quick mutation check).

## Prompts

The files in `prompts/` drive the Claude and Codex children. Before merging a change there, run the prompt eval and include its comparison in the pull request:

```sh
uv run gaffer eval
```

`gaffer eval` runs three toy tickets end to end on your own Claude and ChatGPT subscriptions (no API keys), so it spends a little quota. It is never run in CI. See the README for its exit codes.

## Pull requests

- Keep each pull request to one change, with a description of what changed and why.
- Make sure `uv run pytest` passes. CI runs the full suite and a gitleaks scan on macOS.
- Never commit tokens, real account data or files from `~/.gaffer`, `~/.claude` or `~/.codex`. Use obviously fake values in tests, built at runtime so they don't look like real secrets.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
