# Changelog

All notable changes to gaffer are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- First public release of the v0.1 code.
- `gaffer run <ISSUE>`: Linear issue to reviewed pull request, with a Codex tester, red-first check, Claude writer, local verification, CodeRabbit, CI and Codex review rounds, and `--continue`, `--more-cycles`, `--fresh` and `--adopt-head`.
- `gaffer doctor`: 22 probes of the login, config-dir, sandbox, file-read, write and network controls, rerun automatically when the Claude, Codex or sandbox-runtime version changes.
- `gaffer eval`: a three-ticket prompt eval with stored baselines and regression comparison.
- Test-runner adapters for pytest, Jest, Vitest, PHPUnit and generic JUnit.

The end-to-end `gaffer run` against a real Linear issue and GitHub repository has not been demonstrated yet; see the README's status line.
