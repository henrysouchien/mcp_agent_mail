#!/usr/bin/env bash
set -euo pipefail
uv run python -m mcp_agent_mail.cli capabilities --json |
  python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["canonical_invocation"]=="mcp-agent-mail"'
