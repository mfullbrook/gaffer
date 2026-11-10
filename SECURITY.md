# Security policy

gaffer runs AI coding agents on your machine with access to your repositories, so security reports are welcome and taken seriously.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: go to the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue.

Include what you found, how to reproduce it, and the impact. You should get a reply within a week.

## Scope

In scope:

- A way for a Claude or Codex child, or a command it runs, to read paths on gaffer's deny list, write outside its job directory, reach hosts outside the allowlist, or obtain gaffer's GitHub or Linear token.
- A way for repository content (issue text, code, CodeRabbit comments, CLAUDE.md or AGENTS.md) to change gaffer's own permissions or settings.
- Secrets reaching a push, a PR comment, a log or the report despite the gitleaks scan.
- gaffer starting children on API keys rather than the subscription logins.

Known limitations are listed under "Security model and residual risks" in the [README](README.md). Reports that show one of them is worse than described are also welcome.

## Supported versions

Only the latest commit on `main` is supported.
