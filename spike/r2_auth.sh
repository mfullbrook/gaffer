#!/usr/bin/env bash
# R2 probe: positive subscription-login check against gaffer's config dirs.
# Exit 0 only if both CLIs report a subscription login.
set -u
. "$(dirname "$0")/env.sh"
echo "claude $(clean claude --version)"; echo "$(clean codex --version)"
cj=$(clean claude auth status --json)
echo "$cj" | redact
cx=$(clean codex login status 2>&1); echo "$cx"
ok=0
echo "$cj" | jq -e '.loggedIn==true and .authMethod=="claude.ai" and .apiProvider=="firstParty" and .configDirectory==env.GAFFER_CLAUDE_DIR' >/dev/null || { echo "R2 FAIL: claude"; ok=1; }
[ "$cx" = "Logged in using ChatGPT" ] || { echo "R2 FAIL: codex"; ok=1; }
[ $ok = 0 ] && echo "R2 PASS"
exit $ok
