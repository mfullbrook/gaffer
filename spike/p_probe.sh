#!/usr/bin/env bash
# Experiment 3b probe battery, run inside a sandbox (the Claude writer's Bash
# tool, or sandbox-runtime). Never prints file contents: reads are probed with
# `head -c 1 > /dev/null`. Full output goes to <job>/probe-<tag>.txt; Composer's
# -vvv log to <job>/composer-<tag>.log.
# Usage: p_probe.sh <job> <source-repo> <canary> <tag> [full]
#   full: also the R20 credential/write probes, blocked hosts and git-over-GitHub.
job=$1; src=$2; canary=$3; tag=$4; full=${5:-}
out="$job/probe-$tag.txt"
r() { printf '%-46s %s\n' "$1" "$2"; }
try() { local l=$1; shift; if "$@" >/dev/null 2>&1; then r "$l" ALLOWED; else r "$l" DENIED; fi; }
H=$HOME
main() {
echo "== environment"
r "PATH has Herd bin" "$(printf '%s' "$PATH" | grep -q 'Herd/bin' && echo yes || echo no)"
r "PATH" "$(printf '%s' "$PATH" | sed "s|$H|~|g")"
r "php" "$(command -v php | sed "s|$H|~|g")"
r "composer" "$(command -v composer | sed "s|$H|~|g")"
r "HERD_PHP_84_INI_SCAN_DIR set" "$([ -n "${HERD_PHP_84_INI_SCAN_DIR:-}" ] && echo yes || echo no)"
r "COMPOSER_HOME" "$(printf '%s' "${COMPOSER_HOME:-unset}" | sed "s|$job|<job>|")"
r "COMPOSER_CACHE_DIR" "$(printf '%s' "${COMPOSER_CACHE_DIR:-unset}" | sed "s|$job|<job>|")"
r "TMPDIR" "$(printf '%s' "${TMPDIR:-unset}" | sed "s|$job|<job>|")"
r "proxy env vars" "$(env | grep -ciE '^(https?|all)_proxy=')"
echo "== Herd php/composer"
r "php -r" "$(php -r 'echo PHP_VERSION;' 2>&1 | head -1)"
r "php ini parsed" "$(php -i 2>/dev/null | sed -n 's/^Additional .ini files parsed => //p' | sed "s|$H|~|g")"
r "composer --version" "$(composer --version 2>&1 | head -1)"
echo "== composer install (prefer-dist, lockfile)"
cd "$job/repo" || return 1
composer install --no-progress -vvv > "$job/composer-$tag.log" 2>&1
r "composer install exit" "$?"
r "packages installed" "$(php -r '$j=@json_decode(@file_get_contents("vendor/composer/installed.json"),true); echo $j ? count($j["packages"]) : 0;')"
r "dist downloads via api.github.com" "$(grep -cE '^Downloading https://api\.github\.com' "$job/composer-$tag.log")"
echo "-- hosts Composer requested (count):"
grep -oE 'https?://[A-Za-z0-9.-]+' "$job/composer-$tag.log" | sort | uniq -c | sort -rn
echo "-- failures:"
grep -hE 'CONNECT tunnel failed|response 403|Failed to download|Could not|TransportException' "$job/composer-$tag.log" | sed -E 's/[0-9a-f]{40}/<sha>/g' | sort | uniq -c | sort -rn | head -5
[ "$full" = full ] || return 0
echo "== writes"
try "write job dir"            touch "$job/.p_probe"
try "write source repo"        touch "$src/.p_probe"
try "write \$HOME"             touch "$H/.p_probe"
try "write ~/.gaffer"          touch "$H/.gaffer/.p_probe"
try "write ~/.composer"        touch "$H/.composer/.p_probe"
echo "== credential reads (one byte, discarded)"
try "read canary"              head -c 1 "$canary"
try "stat canary"              stat "$canary"
for p in .ssh/id_ed25519 .ssh/config .config/gh/hosts.yml Library/Keychains/login.keychain-db \
         .claude/.credentials.json .claude.json .codex/auth.json .gaffer/claude/.credentials.json \
         .gaffer/codex/auth.json .composer/latest.phar \
         "Library/Application Support/Herd/config/valet/CA/LaravelValetCASelfSigned.key"; do
  try "read ~/$p" head -c 1 "$H/$p"
done
try "read job file"            head -c 1 "$job/repo/composer.json"
try "read Herd php.ini"        head -c 1 "$H/Library/Application Support/Herd/config/php/84/php.ini"
echo "== network"
for u in https://repo.packagist.org/packages.json https://api.github.com/zen https://codeload.github.com/ \
         https://example.com/ https://github.com/ https://objects.githubusercontent.com/ https://pypi.org/simple/pip/; do
  r "GET $u" "$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "$u" 2>/dev/null)"
done
try "git ls-remote origin (fake GitHub)" git ls-remote origin
try "git ls-remote github.com/Seldaek/monolog" git ls-remote https://github.com/Seldaek/monolog.git
echo "== composer install --prefer-source (fresh clone in job)"
rm -rf "$job/repo-src"; git clone -q "$job/repo" "$job/repo-src" && cd "$job/repo-src" || return 1
composer install --prefer-source --no-progress -vvv > "$job/composer-$tag-source.log" 2>&1
r "composer install --prefer-source exit" "$?"
grep -hE 'Failed to clone|denying|403' "$job/composer-$tag-source.log" | sed -E 's#https://github.com/[^ ]+#https://github.com/<pkg>#' | sort | uniq -c | sort -rn | head -3
}
# Plain pipe, not process substitution: /dev/fd is denied under sandbox-runtime.
main 2>&1 | tee "$out"
