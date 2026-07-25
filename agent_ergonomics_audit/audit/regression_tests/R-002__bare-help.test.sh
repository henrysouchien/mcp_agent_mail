#!/usr/bin/env bash
set -euo pipefail
set +e
output=$(uv run python -m mcp_agent_mail.cli 2>&1)
status=$?
set -e
[[ ${status} -eq 2 ]]
grep -q "Usage:" <<<"${output}"
! grep -q "address already in use" <<<"${output}"
