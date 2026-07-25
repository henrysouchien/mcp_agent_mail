#!/usr/bin/env bash
set -euo pipefail
set +e
output=$(mcp-agent-mail 2>&1)
status=$?
set -e
[[ ${status} -eq 2 ]]
grep -q "Usage: mcp-agent-mail" <<<"${output}"
! grep -q "Usage: python -m" <<<"${output}"
