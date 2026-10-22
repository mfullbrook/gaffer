#!/usr/bin/env bash
# Experiment 3b step 3a/5: the sandboxed Claude writer runs p_probe.sh in a
# fresh PHP job clone. One small model run per host set.
# Usage: p_writer.sh <toy> <hosts> <tag> <out-prefix> [full]
set -eu
. "$(dirname "$0")/env.sh"; . "$SPIKE_DIR/p_env.sh"
toy=$1; hosts=$2; tag=$3; out=$4; full=${5:-}
canary_dir="$HOME/.gaffer-spike-canary"; canary="$canary_dir/fake_token"
mkdir -p "$canary_dir"; echo "CANARY-NOT-A-REAL-SECRET" > "$canary"
job=$(php_job "$toy")
ALLOWED_DOMAINS=$hosts "$SPIKE_DIR/writer_settings.sh" "$job" "${LOCKDOWN:--}" \
  "$HOME/.claude.json" "$HOME/.composer" "$HOME/.config/composer" \
  "$HOME/Library/Application Support/Herd/config/valet" "$canary_dir" > "$out.settings.json"
echo "job=$job"
prompt="Run exactly this one command with the Bash tool (no changes, no retries, no workarounds), then reply with only the word DONE:
bash $SPIKE_DIR/p_probe.sh $job $toy $canary $tag $full"
cd "$job/repo"
# gaffer's launcher puts Herd's bin dir on PATH and Composer's home and cache in the job dir.
/usr/bin/env -i HOME="$HOME" PATH="$PHP_PATH" USER="${USER:-$(id -un)}" TMPDIR="$job/tmp" \
  LANG="${LANG:-en_GB.UTF-8}" TERM="${TERM:-xterm-256color}" \
  CLAUDE_CONFIG_DIR="$GAFFER_CLAUDE_DIR" CODEX_HOME="$GAFFER_CODEX_DIR" \
  COMPOSER_HOME="$job/composer-home" COMPOSER_CACHE_DIR="$job/composer-home/cache" COMPOSER_NO_INTERACTION=1 \
  ENABLE_CLAUDEAI_MCP_SERVERS=false CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1 CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 \
  BASH_DEFAULT_TIMEOUT_MS=600000 BASH_MAX_TIMEOUT_MS=600000 \
  claude -p "$prompt" --output-format stream-json --verbose \
  --settings "$out.settings.json" --setting-sources user \
  --mcp-config '{"mcpServers":{}}' --strict-mcp-config \
  --tools "Bash,Read,Edit,Write,Skill" --permission-mode acceptEdits \
  --model sonnet --max-turns 4 < /dev/null > "$out.jsonl" 2> "$out.stderr" || echo "claude exit=$?"
jq -c 'select(.type=="system" and .subtype=="init") | {claude_code_version, plugins: [.plugins[]?.name], mcp_servers}' "$out.jsonl"
jq -c 'select(.type=="result") | {subtype, is_error, num_turns, result: (.result|.[0:200])}' "$out.jsonl"
jq -c 'select(.type=="rate_limit_event") | .rate_limit_info // .' "$out.jsonl" | tail -1
echo "--- probe output ($job/probe-$tag.txt):"
cat "$job/probe-$tag.txt" 2>/dev/null || echo "(missing: probe did not run)"
echo "$job" > "$out.job"
