# Phase 0 Scope Decision

```yaml
mode: full
target_kind: paired_cli_and_mcp_server
target_path: /Users/henrychien/.local/share/mcp_agent_mail
target_branch: main
audit_workspace: /Users/henrychien/.local/share/mcp_agent_mail/agent_ergonomics_audit
primary_agent_profile: claude_code
secondary_agent_profiles:
  - codex_cli
  - grok_cli
mcp_transport: streamable_http
companion_cli: uv run python -m mcp_agent_mail.cli
triangulation: none
cass_mining: skipped_missing_optional_skill
toolchain_install_policy: ask_first
```

## Canonical tasks

1. Discover the safest next command or MCP call without external documentation.
2. Start or resume an authenticated tmux-managed agent session.
3. Verify server identity, runtime incarnation, and pane route without mistaking
   omitted evidence for missing state.
4. Diagnose a broken identity or route and receive one safe, copyable recovery
   action.
5. Inspect MCP capabilities and schemas in a stable machine-readable format.
6. Use CLI read surfaces non-interactively with deterministic structured output.

## Scope guardrails

- Improve CLI and MCP agent ergonomics; do not bundle unrelated feature work.
- Preserve all unrelated tracked and untracked changes.
- Do not change `.env`.
- Do not create or switch branches or worktrees.
- Do not delete files.
- Do not install additional toolchains without asking.
- Use native project lint, type, and test gates.
- Deploy and live-test the managed launchd service only after source validation.

## Fallbacks

- Solo execution: no subagents or parallel writers.
- Missing CASS, UBS, Beads, and triangulation helpers use the documented native
  analysis, project-linter, recommendations JSONL, and manual-review fallbacks.
