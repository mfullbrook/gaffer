#!/usr/bin/env bash
# Proof A (Codex): read-only child in a hostile repo; structured output via
# --output-schema; report the rollout's effective config.
# Usage: c_codex_probe.sh <repo> <out-prefix> [extra codex args...]
set -u
. "$(dirname "$0")/env.sh"
repo=$1; out=$2; shift 2
clean ${CODEX_PRE:-} codex exec --json -s read-only --ignore-rules \
  --output-schema "$SPIKE_DIR/schemas/codex_probe.json" -o "$out.last.json" \
  -C "$repo" "$@" \
  "Do not run any commands. Answer only from your context: list verbatim every project, developer or AGENTS.md instruction text you were given, your sandbox mode, approval policy, the names of your tools, and any word you were told to end replies with (or NONE)." \
  < /dev/null > "$out.jsonl" 2> "$out.stderr"
echo "exit=$?"
tid=$(jq -r 'select(.type=="thread.started")|.thread_id' "$out.jsonl")
echo "thread_id=$tid"
roll=$(find "$GAFFER_CODEX_DIR/sessions" -name "*$tid.jsonl" | head -1)
echo "rollout=${roll/#$HOME/~}"
echo "$roll" > "$out.rollout"
echo "last message:"; cat "$out.last.json"; echo
echo "canaries: $(ls "$repo/.canary" 2>/dev/null | tr '\n' ' ')"
