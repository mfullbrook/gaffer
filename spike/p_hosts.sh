#!/usr/bin/env bash
# Summarise every host a Composer -vvv run tried, and what the sandbox did.
# Composer's -vvv output goes to stderr, which p_srt_run.sh saves with srt's
# debug log in <out>.debug. Usage: p_hosts.sh <out-prefix>
out=$1
echo "-- URLs Composer requested (host + first path segment, count):"
grep -hoE 'https?://[A-Za-z0-9.-]+(/[A-Za-z0-9._-]*)?' "$out.debug" | sort | uniq -c | sort -rn
echo "-- api.github.com requests: $(grep -cE '^Downloading https://api\.github\.com' "$out.debug")"
echo "-- sandbox network decisions:"
grep -hE 'Allowed by config rule|denying:' "$out.debug" | sed -E 's/\[SandboxDebug\] //' | sort | uniq -c | sort -rn
