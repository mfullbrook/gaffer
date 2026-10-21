#!/usr/bin/env bash
# Proof C (install_command): fresh job clone, then run the ecosystem's install
# under the Codex `install` profile (registry hosts only, writes = job dir).
# Usage: c_install.sh <toy-repo> <py|js>   -> prints JOB=<dir>
set -u
. "$(dirname "$0")/env.sh"
src=$1; eco=$2
export JOB=$(mktemp -d -t "gaffer-spike-job2-$eco")
git clone -q --local "$src" "$JOB/repo"; git -C "$JOB/repo" remote set-url origin "https://github.com/gaffer-spike/toy-$eco.git"
mkdir -p "$JOB/tmp"
DENY_EXTRA="$HOME/.gaffer-spike-canary"; . "$SPIKE_DIR/codex_profiles.sh"
if [ "$eco" = py ]; then
  cmd='python3 -m venv .venv && .venv/bin/pip install -q --no-cache-dir pytest && .venv/bin/python -m pytest --version'
else
  cmd='npm ci --no-audit --no-fund --cache "$JOB/.npm-cache" >/dev/null && ls node_modules/.bin | grep -E "^jest$" && ls node_modules | grep -c . '
fi
cd "$JOB/repo"
clean TMPDIR="$JOB/tmp" JOB="$JOB" codex sandbox "${CODEX_PROFILE_ARGS_install[@]}" -P gaffer_install -C "$JOB/repo" -- /bin/bash -c "$cmd" < /dev/null 2>&1 | tail -5
echo "install exit=${PIPESTATUS[0]}"
echo "JOB=$JOB"
