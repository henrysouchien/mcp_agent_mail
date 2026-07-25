#!/usr/bin/env bash
set -euo pipefail
uv run python -m mcp_agent_mail.cli mail status . --json |
  python3 -c 'import json,sys; p=json.load(sys.stdin); assert isinstance(p["worktrees_enabled"], bool); assert p["slug"]'
