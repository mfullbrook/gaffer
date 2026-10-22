#!/usr/bin/env bash
# Print the writer's --settings JSON for one job (R1, R3, R14, R21).
# Usage: writer_settings.sh <job-dir> <lockdown.json|-> [extra-denyRead-dir...]
#   lockdown.json: enabledPlugins/skillOverrides fragment from gen_lockdown.sh.
#   ALLOWED_DOMAINS (env, comma-separated) overrides the registry hosts (Experiment 3b).
set -eu
job=$1; lock=$2; shift 2
H=$HOME
deny=("$H/.ssh" "$H/.config/gh" "$H/.aws" "$H/Library/Keychains" "$H/.claude" "$H/.codex" "$H/.gaffer" "$@")
denyjson=$(printf '%s\n' "${deny[@]}" | jq -R . | jq -sc .)
# File-tool rules: "//abs/path/**" is an absolute path in permission syntax.
readdeny=$(printf '%s\n' "${deny[@]}" | jq -R '"Read(/" + . + "/**)"' | jq -sc .)
domains=${ALLOWED_DOMAINS:-pypi.org,files.pythonhosted.org,registry.npmjs.org}
base=$(jq -n --arg job "$job" --arg domains "$domains" --argjson deny "$denyjson" --argjson readdeny "$readdeny" '{
  permissions: {
    deny: (["WebFetch", "WebSearch", "mcp__claude_ai_Linear"] + $readdeny),
    additionalDirectories: []
  },
  disableAllHooks: true,
  sandbox: {
    enabled: true,
    failIfUnavailable: true,
    autoAllowBashIfSandboxed: true,
    allowUnsandboxedCommands: false,
    filesystem: { allowWrite: [$job], denyRead: $deny },
    network: { allowedDomains: ($domains | split(",") | map(select(. != ""))) }
  }
}')
if [ "$lock" = "-" ]; then echo "$base"; else jq -s '.[0] * (.[1] | {enabledPlugins, skillOverrides, disableBundledSkills})' <(echo "$base") "$lock"; fi
