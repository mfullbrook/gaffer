#!/usr/bin/env bash
# Experiment 3b: sandbox-runtime settings for gaffer's install_command (R14, R20, R28).
# Usage: p_srt_settings.sh <job-dir> <comma-separated hosts>
set -eu
. "$(dirname "$0")/env.sh"; . "$SPIKE_DIR/p_env.sh"
job=$1; hosts=$2
jq -n --arg job "$job" --arg hosts "$hosts" --argjson deny "$(php_deny_list | jq -R . | jq -sc .)" '{
  network: {allowedDomains: ($hosts | split(",") | map(select(. != ""))), deniedDomains: []},
  filesystem: {denyRead: $deny, allowWrite: [$job], denyWrite: []}
}'
