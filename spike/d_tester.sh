#!/usr/bin/env bash
# Proof D: run the Codex tester read-only (R20 `ro` profile) in a job clone; it
# returns a criterion map plus test file contents as structured output (R5, R22).
# Usage: d_tester.sh <job-dir> <py|js> <out-prefix>
set -u
. "$(dirname "$0")/env.sh"
export JOB=$1; eco=$2; out=$3
DENY_EXTRA="$HOME/.gaffer-spike-canary"; . "$SPIKE_DIR/codex_profiles.sh"; . "$SPIKE_DIR/codex_child_args.sh"
if [ "$eco" = py ]; then
  ticket='TOY-1. (1) Add word_count(text) to toy/strings.py: returns the number of whitespace-separated words, 0 for empty or whitespace-only text. (2) shout(text) must strip leading and trailing whitespace before upper-casing. (3) shout must keep upper-casing inner text.'
  conv='Runner: pytest (JUnit xunit2). test_dirs: ["tests"]. Identity: file = repo-relative path; classname = dotted module path without .py (e.g. tests.test_x), plus .ClassName if inside a class; name = test function name. Import the functions under test at module top level.'
else
  ticket='TOY-1. (1) Add wordCount(text) to src/strings.js (exported): returns the number of whitespace-separated words, 0 for empty or whitespace-only text. (2) shout(text) must trim leading and trailing whitespace before upper-casing. (3) shout must keep upper-casing inner text.'
  conv='Runner: Jest with jest-junit (classname = repo-relative file path; name = enclosing describe titles and the test title joined by single spaces). test_dirs: ["__tests__"]. Import with require at the top of the file.'
fi
prompt="You are the tester. Read the repository, then write failing-first tests for this ticket. Do not modify any files; return everything in the structured output.
Ticket: $ticket
$conv
Rules: one new test file per function under test; label each test acceptance (must fail on the current code) or guard (protects existing behaviour, may already pass); at least one acceptance test per criterion that changes behaviour; list in expected_missing every symbol your tests use that does not exist yet; every test you write must appear in tests with its exact identity. Keep it to at most 5 tests."
cd "$JOB/repo"
clean HOME="$JOB/tmp/home" codex "${CODEX_CHILD_ARGS[@]}" --output-schema "$SPIKE_DIR/schemas/tester.json" -o "$out.tester.json" \
  -C "$JOB/repo" "$prompt" < /dev/null > "$out.jsonl" 2> "$out.stderr"
echo "tester exit=$?"
tid=$(jq -r 'select(.type=="thread.started")|.thread_id' "$out.jsonl"); find "$GAFFER_CODEX_DIR/sessions" -name "*$tid.jsonl" > "$out.rollout"
jq -c '{criteria: [.criteria[].id], tests: [.tests[] | [.label, .file, .classname, .name]], expected_missing, files: [.files[].path]}' "$out.tester.json"
