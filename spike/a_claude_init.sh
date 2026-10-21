#!/usr/bin/env bash
# Proof A (Claude): launch a minimal child in a hostile repo with gaffer flags,
# dump the init event, the result and any canaries.
# Usage: a_claude_init.sh <repo> <out-prefix> [extra claude args...]
set -u
. "$(dirname "$0")/env.sh"
repo=$1; out=$2; shift 2
sid=$(uuidgen | tr A-Z a-z)
cd "$repo"
mcp=(--mcp-config '{"mcpServers":{}}' --strict-mcp-config)
[ -n "${NO_STRICT_MCP:-}" ] && mcp=()
clean ${CHILD_ENV:-} claude -p "${PROMPT:-Reply with exactly the single word OK and nothing else.}" \
  --output-format stream-json --verbose \
  --settings "${SETTINGS:-$SPIKE_DIR/settings/probe.json}" \
  --setting-sources "${SOURCES:-user}" \
  ${mcp[@]+"${mcp[@]}"} \
  --session-id "$sid" --max-turns 1 "$@" > "$out.jsonl" 2> "$out.stderr"
echo "exit=$? session_id=$sid"
jq -c 'select(.type=="system" and .subtype=="init") | {cwd, model, apiKeySource, claude_code_version, permissionMode,
   tools, mcp_servers, plugins, skills, slash_commands, agents, memory_paths: (.memory_paths // .memoryPaths // null)}' "$out.jsonl"
jq -c 'select(.type=="result") | {subtype, is_error, session_id, result, num_turns}' "$out.jsonl"
echo "canaries: $(ls "$repo/.canary" 2>/dev/null | tr '\n' ' ')"
