#!/usr/bin/env bash
set -euo pipefail
uv run python - <<'PY'
import asyncio
from fastmcp import Client
from mcp_agent_mail.app import build_mcp_server

async def main():
    async with Client(build_mcp_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert "mutating operation" in tools["uninstall_precommit_guard"].description
        assert "Do not call merely" in tools["reconcile_runtime_binding"].description

asyncio.run(main())
PY
