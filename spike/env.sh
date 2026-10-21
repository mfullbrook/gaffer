# Shared spike environment. Source this; do not execute it.
# Children start through `clean` (env -i allowlist, as in Experiment 1) with
# gaffer's dedicated config dirs (R3).
export GAFFER_CLAUDE_DIR="$HOME/.gaffer/claude"
export GAFFER_CODEX_DIR="$HOME/.gaffer/codex"
SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export SPIKE_DIR
CLEAN_PATH="$HOME/.local/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

clean() {
  /usr/bin/env -i HOME="$HOME" PATH="$CLEAN_PATH" USER="${USER:-$(id -un)}" TMPDIR="${TMPDIR:-/tmp}" \
    LANG="${LANG:-en_GB.UTF-8}" TERM="${TERM:-xterm-256color}" \
    CLAUDE_CONFIG_DIR="$GAFFER_CLAUDE_DIR" CODEX_HOME="$GAFFER_CODEX_DIR" "$@"
}

# Strip identifying fields from auth JSON / text.
redact() {
  sed -E 's/"(email|emailAddress|orgId|orgName|organization[A-Za-z]*|accountUuid|userId)": *"[^"]*"/"\1": "<redacted>"/g; s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<redacted-email>/g'
}
