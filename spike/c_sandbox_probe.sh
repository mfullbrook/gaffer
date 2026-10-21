#!/usr/bin/env bash
# Proof C: run the read/write/network probe under `codex sandbox -P <profile>`
# (no model call). Never prints file contents.
# Usage: c_sandbox_probe.sh <job-dir> <source-repo> <canary-file> <ro|test|install>
set -u
. "$(dirname "$0")/env.sh"
export JOB=$1; src=$2; canary=$3; role=$4
DENY_EXTRA="$(dirname "$canary")"
. "$SPIKE_DIR/codex_profiles.sh"
if [ "$role" = default ]; then args=(); else eval "args=(\"\${CODEX_PROFILE_ARGS_$role[@]}\" -P gaffer_$role)"; fi
probe=$(cat <<'EOF'
r() { printf '%-44s %s\n' "$1" "$2"; }
try() { local l=$1; shift; if "$@" >/dev/null 2>&1; then r "$l" ALLOWED; else r "$l" DENIED; fi; }
try "write job dir"        touch "$JOB/.c_probe"
try "write source repo"    touch "$SRC/.c_probe"
try "write \$HOME"         touch "$HOME/.c_probe"
try "read canary"          head -c 1 "$CANARY"
try "stat canary"          stat "$CANARY"
for p in .ssh/id_ed25519 .ssh/config .config/gh/hosts.yml Library/Keychains/login.keychain-db \
         .claude/.credentials.json .claude.json .codex/auth.json .gaffer/claude/.credentials.json .gaffer/codex/auth.json; do
  try "read ~/$p" head -c 1 "$HOME/$p"
done
try "read job file"        head -c 1 "$JOB/repo/.gitignore"
try "read source repo file" head -c 1 "$SRC/.gitignore"
for u in https://pypi.org/simple/pip/ https://registry.npmjs.org/left-pad https://api.github.com/zen https://example.com/; do
  r "GET $u" "$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "$u" 2>/dev/null)"
done
r "proxy env" "$(env | grep -ciE '^(https?|all)_proxy=')"
EOF
)
cd "$JOB"; mkdir -p "$JOB/tmp"
clean TMPDIR="$JOB/tmp" JOB="$JOB" SRC="$src" CANARY="$canary" codex sandbox ${args[@]+"${args[@]}"} -C "$JOB" -- /bin/bash -c "$probe" < /dev/null
echo "exit=$?"
