#!/usr/bin/env bash
# Sandbox probe battery, run by the writer's Bash tool inside the job clone.
# Never prints file contents: reads are probed with `head -c 1` (opens the file,
# reads nothing) and `stat`. Usage: b_probe.sh <source-repo> <canary-file> <ecosystem: py|js>
src=$1; canary=$2; eco=$3
job=$(pwd)
r() { printf '%-44s %s\n' "$1" "$2"; }
try() { local label=$1; shift; if "$@" >/dev/null 2>&1; then r "$label" ALLOWED; else r "$label" DENIED; fi; }

echo "## env"; r "pwd" "$job"; r "TMPDIR" "${TMPDIR:-}"; r "sandbox env" "$(env | grep -ciE '^(SANDBOX|CLAUDE_CODE_SANDBOX)')"
echo "## writes"
try "write job clone"             touch "$job/.probe_write"
try "write source repo"           touch "$src/.probe_write"
try "write source .git/hooks"     touch "$src/.git/hooks/probe"
try "write \$HOME"                touch "$HOME/.probe_write"
try "write ~/.gaffer"             touch "$HOME/.gaffer/.probe_write"
echo "## reads (head -c 1 opens the file; stat reads metadata only)"
try "read canary (head -c 1)"     head -c 1 "$canary"
try "stat canary"                 stat "$canary"
try "list canary dir"             ls "$(dirname "$canary")"
for p in "$HOME/.ssh/id_ed25519" "$HOME/.ssh/id_rsa" "$HOME/.ssh/config" "$HOME/.config/gh/hosts.yml" \
         "$HOME/.aws/credentials" "$HOME/Library/Keychains/login.keychain-db" "$HOME/.claude/.credentials.json" \
         "$HOME/.claude.json" "$HOME/.codex/auth.json" "$HOME/.gaffer/claude/.credentials.json" "$HOME/.gaffer/codex/auth.json"; do
  if [ -e "$p" ] 2>/dev/null || stat "$p" >/dev/null 2>&1; then try "read ${p/#$HOME/~}" head -c 1 "$p"
  else r "read ${p/#$HOME/~}" "absent-or-unstatable"; fi
done
try "ls ~/.ssh"                    ls "$HOME/.ssh"
echo "## network"
for u in https://pypi.org/simple/pip/ https://files.pythonhosted.org/ https://registry.npmjs.org/left-pad \
         https://api.github.com/zen https://example.com/ https://github.com/; do
  code=$(curl -sS -m 15 -o /dev/null -w '%{http_code}' "$u" 2>&1 | tail -1)
  r "GET $u" "$code"
done
try "git ls-remote origin (fake GitHub)" timeout 20 git ls-remote origin
echo "## dependency install ($eco)"
if [ "$eco" = py ]; then
  python3 -m venv .venv >/dev/null 2>&1 && r "venv" ok || r "venv" FAIL
  pipi() { local label=$1; shift; if "$@" > .pip.log 2>&1; then r "$label" "ok"; else r "$label" "FAIL: $(grep -m1 -oE 'SSLCertVerificationError[^:]*|ERROR: .*' .pip.log)"; fi; }
  pipi "pip install pytest (default truststore)" .venv/bin/pip install -q --no-cache-dir pytest
  pipi "pip install --use-deprecated=legacy-certs" .venv/bin/pip install -q --no-cache-dir --use-deprecated=legacy-certs pytest
  pipi "uv pip install (bundled roots)" env UV_CACHE_DIR="$job/.uv-cache" uv pip install -q --python .venv/bin/python iniconfig
  r "pytest importable" "$(.venv/bin/python -m pytest --version 2>&1 | head -1)"
else
  if npm install --no-audit --no-fund --cache "$job/.npm-cache" > .npm.log 2>&1; then
    r "npm install (jest, jest-junit)" "ok ($(ls node_modules | wc -l | tr -d ' ') packages)"
  else r "npm install" "FAIL: $(tail -3 .npm.log | tr '\n' ' ')"; fi
fi
echo "## git commit in job clone"
echo "probe $(date -u +%FT%TZ)" > PROBE.txt
git add PROBE.txt && git -c user.name=gaffer-writer -c user.email=writer@gaffer.invalid commit -qm "probe: sandboxed commit" \
  && r "git commit" "ok $(git rev-parse --short HEAD)" || r "git commit" FAIL
