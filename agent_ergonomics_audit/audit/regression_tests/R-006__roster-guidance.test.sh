#!/usr/bin/env bash
set -euo pipefail
rg -q '"interpretation": interpretation' src/mcp_agent_mail/app.py
rg -q '"recommended_action": recommended_action' src/mcp_agent_mail/app.py
