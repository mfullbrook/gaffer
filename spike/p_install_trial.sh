#!/usr/bin/env bash
# Experiment 3b step 3b/4: one `composer install` trial under sandbox-runtime
# in a fresh job clone (empty Composer cache). Usage:
#   p_install_trial.sh <toy> <hosts> <out-prefix> [extra composer args...]
set -u
. "$(dirname "$0")/env.sh"; . "$SPIKE_DIR/p_env.sh"
toy=$1; hosts=$2; out=$3; shift 3
job=$(php_job "$toy")
"$SPIKE_DIR/p_srt_run.sh" "$job" "$hosts" "$out" "composer install --no-progress -vvv $*"
rc=$?
echo "# hosts=$hosts args=[$*] job=$job"
echo "composer exit=$rc; packages installed: $(jq ".packages | length" "$job/repo/vendor/composer/installed.json" 2>/dev/null || echo 0)"
"$SPIKE_DIR/p_hosts.sh" "$out"
grep -hE '^(  - |Failed|In |  \[|.*(rate limit|API limit|Could not|failed to open))' "$out.debug" | grep -viE 'Downloading|Installing|Reading' | sort | uniq -c | sort -rn | head -8
echo "$job" > "$out.job"
