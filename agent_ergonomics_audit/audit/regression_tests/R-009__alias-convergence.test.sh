#!/usr/bin/env bash
set -euo pipefail
for alias_name in mcp_agent_mail mcpagentmail agentmail agent-mail; do
  resolved=$(readlink "/Users/henrychien/.local/bin/${alias_name}")
  [[ ${resolved} == "/Users/henrychien/.local/bin/mcp-agent-mail" ]]
done
