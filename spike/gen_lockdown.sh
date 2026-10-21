#!/usr/bin/env bash
# Build a settings fragment that turns off every plugin and every skill the
# init event reported except the allowlist. Usage: gen_lockdown.sh <init.jsonl> <allowed-skill>...
set -eu
init=$1; shift
allow=$(printf '%s\n' "$@" | jq -R . | jq -sc .)
jq -c --argjson allow "$allow" 'select(.type=="system" and .subtype=="init") |
  { enabledPlugins: ([.plugins[].source | {key: ., value: false}] | from_entries),
    skillOverrides: ([.skills[] | select(. as $s | $allow | index($s) | not) | {key: ., value: "off"}] | from_entries),
    disableBundledSkills: true }' "$init"
