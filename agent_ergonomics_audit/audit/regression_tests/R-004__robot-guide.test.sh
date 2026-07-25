#!/usr/bin/env bash
set -euo pipefail
uv run python -m mcp_agent_mail.cli robot-docs guide --json |
  python3 -c 'import json,sys; p=json.load(sys.stdin); assert any("verification_not_requested" in x for x in p["identity"])'
