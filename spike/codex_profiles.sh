# Codex permission profiles for R20, as `-c` overrides. Source this; it sets
# CODEX_PROFILE_ARGS_<role> arrays. Requires: JOB (job dir), DENY_EXTRA (optional dir).
# Roles:
#   ro      - tester/reviewer children: read everything except credentials, write nothing, no network
#   test    - gaffer test_command: writes only the job dir (TMPDIR must point inside it), credentials denied, no network
#   install - gaffer install_command: as test, plus registry hosts via Codex's proxy
_codex_deny_entries() {
  local p
  for p in "$HOME/.ssh" "$HOME/.config/gh" "$HOME/.aws" "$HOME/Library/Keychains" \
           "$HOME/.claude" "$HOME/.claude.json" "$HOME/.codex" "$HOME/.gaffer" ${DENY_EXTRA:-}; do
    printf ',"%s"="deny"' "$p"
  done
}
_deny=$(_codex_deny_entries)
CODEX_PROFILE_ARGS_ro=(
  -c 'default_permissions="gaffer_ro"'
  -c "permissions.gaffer_ro.filesystem={\":root\"=\"read\"$_deny}"
  -c 'permissions.gaffer_ro.network.enabled=false'
)
CODEX_PROFILE_ARGS_test=(
  -c 'default_permissions="gaffer_test"'
  -c "permissions.gaffer_test.filesystem={\":root\"=\"read\",\"$JOB\"=\"write\"$_deny}"
  -c 'permissions.gaffer_test.network.enabled=false'
)
CODEX_PROFILE_ARGS_install=(
  -c 'default_permissions="gaffer_install"'
  -c "permissions.gaffer_install.filesystem={\":root\"=\"read\",\"$JOB\"=\"write\"$_deny}"
  --enable network_proxy
  -c 'permissions.gaffer_install.network.enabled=true'
  -c 'permissions.gaffer_install.network.domains={"pypi.org"="allow","files.pythonhosted.org"="allow","registry.npmjs.org"="allow"}'
)
