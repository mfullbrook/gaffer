#!/usr/bin/env bash
# Experiment 3b: run a command string under sandbox-runtime with gaffer's
# install env (Herd on PATH, COMPOSER_HOME/CACHE and TMPDIR in the job dir).
# srt itself keeps the short system TMPDIR: it binds Unix sockets there, and a
# job-dir path overflows macOS's 104-byte socket path limit (listen EINVAL).
# Usage: p_srt_run.sh <job-dir> <hosts> <out-prefix> <command string>
set -u
. "$(dirname "$0")/env.sh"; . "$SPIKE_DIR/p_env.sh"
job=$1; hosts=$2; out=$3; cmd=$4
"$SPIKE_DIR/p_srt_settings.sh" "$job" "$hosts" > "$out.srt.json"
cd "$job/repo"
/usr/bin/env -i HOME="$HOME" PATH="$PHP_PATH" USER="${USER:-$(id -un)}" TMPDIR="${TMPDIR:-/tmp}" \
  LANG="${LANG:-en_GB.UTF-8}" TERM=dumb COMPOSER_HOME="$job/composer-home" \
  COMPOSER_CACHE_DIR="$job/composer-home/cache" COMPOSER_NO_INTERACTION=1 JOB="$job" \
  "$SRT" --debug --settings "$out.srt.json" -c "export TMPDIR=\"$job/tmp\"; $cmd" < /dev/null > "$out.log" 2> "$out.debug"
rc=$?
echo "exit=$rc"
exit $rc
