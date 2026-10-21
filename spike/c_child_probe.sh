#!/bin/bash
# Run by a Codex child: reports read results only (one byte to /dev/null), never contents.
for p in "/Users/mark/.gaffer-spike-canary/fake_token" "/Users/mark/.ssh/config" "/Users/mark/.ssh/id_ed25519" \
         "/Users/mark/.config/gh/hosts.yml" "/Users/mark/.gaffer/codex/auth.json" "/Users/mark/.codex/auth.json" .gitignore; do
  if head -c 1 "$p" >/dev/null 2>&1; then echo "READABLE $p"; else echo "DENIED   $p"; fi
done
