# Common Codex child flags (tester/reviewer). Source after codex_profiles.sh.
CODEX_CHILD_ARGS=(
  exec --json --ignore-rules
  "${CODEX_PROFILE_ARGS_ro[@]}"
  -c project_doc_max_bytes=0
  -c skills.include_instructions=false
  -c 'web_search="disabled"'
  --disable multi_agent --disable apps --disable plugins --disable browser_use --disable computer_use
  --disable hooks --disable image_generation --disable in_app_browser --disable remote_plugin --disable goals
)
