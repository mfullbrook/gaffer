#!/usr/bin/env bash
# Proof E (R9): launch or resume the sandboxed Claude writer by a pre-assigned
# session ID, with the full writer flag set from Proofs A and B.
# Usage: e_writer.sh <job-dir> <session-id> <new|resume> <prompt> <out-prefix> [lockdown.json]
set -u
. "$(dirname "$0")/env.sh"
job=$1; sid=$2; mode=$3; prompt=$4; out=$5; lock=${6:--}
"$SPIKE_DIR/writer_settings.sh" "$job" "$lock" "$HOME/.gaffer-spike-canary" > "$out.settings.json"
if [ "$mode" = new ]; then idflag=(--session-id "$sid"); else idflag=(--resume "$sid"); fi
cd "$job/repo"
clean ENABLE_CLAUDEAI_MCP_SERVERS=false CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1 CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 \
  BASH_DEFAULT_TIMEOUT_MS=600000 BASH_MAX_TIMEOUT_MS=600000 \
  claude -p "$prompt" --output-format stream-json --verbose \
  --settings "$out.settings.json" --setting-sources user \
  --mcp-config '{"mcpServers":{}}' --strict-mcp-config \
  --tools "Bash,Read,Edit,Write,Skill" --permission-mode acceptEdits \
  --model sonnet --max-turns 30 "${idflag[@]}" < /dev/null > "$out.jsonl" 2> "$out.stderr"
echo "claude exit=$? pid=$$"
jq -c 'select(.type=="system" and .subtype=="init") | {session_id, tools, mcp_servers, plugins: [.plugins[].source], skills}' "$out.jsonl"
jq -c 'select(.type=="result") | {subtype, is_error, session_id, num_turns, result: (.result|.[0:600])}' "$out.jsonl"
echo "HEAD: $(git log --oneline -1)  status: $(git status --porcelain | wc -l | tr -d ' ') dirty"
echo "canaries: $(ls "$job/repo/.canary" 2>/dev/null | tr '\n' ' ')"
