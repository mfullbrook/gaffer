# Experiment 3b additions to env.sh. Source after env.sh.
# Herd's php and composer live in a directory with a space in its name.
HERD_BIN="$HOME/Library/Application Support/Herd/bin"
SRT="$HOME/.gaffer/tools/sandbox-runtime/node_modules/.bin/srt"
PHP_PATH="$HERD_BIN:$CLEAN_PATH"
# R20 deny list (as amended by R28) plus the 3b additions flagged in the write-up:
# Composer's global auth.json locations and Herd's local CA private key.
php_deny_list() {
  printf '%s\n' "$HOME/.ssh" "$HOME/.config/gh" "$HOME/.aws" "$HOME/Library/Keychains" \
    "$HOME/.claude" "$HOME/.claude.json" "$HOME/.codex" "$HOME/.gaffer" \
    "$HOME/.composer" "$HOME/.config/composer" \
    "$HOME/Library/Application Support/Herd/config/valet" ${DENY_EXTRA:-}
}
# New per-job clone with a fake GitHub origin. Prints the job dir.
php_job() {
  local toy=$1 job
  job=$(mktemp -d -t gaffer-spike-job-php)
  git clone -q --local "$toy" "$job/repo"
  git -C "$job/repo" remote set-url origin "https://github.com/gaffer-spike/toy-php.git"
  git -C "$job/repo" config user.name "gaffer"; git -C "$job/repo" config user.email "gaffer@gaffer.invalid"
  mkdir -p "$job/tmp" "$job/composer-home"
  echo "$job"
}
