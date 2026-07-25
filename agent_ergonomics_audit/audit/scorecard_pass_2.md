# Agent Ergonomics Scorecard — Pass 2

Baseline inventory: 339 paired CLI/MCP surfaces. Post-change discovery: 68 CLI command nodes, 156 CLI parameters, 64 MCP tools, 7 exact resources, and 21 resource templates.

## Materially improved surfaces

| Surface | Baseline | Pass 2 | Uplift | Evidence |
|---|---:|---:|---:|---|
| canonical `mcp-agent-mail` executable | 227 | 930 | +703 | real dispatch, argv/exit preservation, live regression |
| bare invocation | 250 | 920 | +670 | help only; no daemon or storage lock |
| `capabilities --json` | absent | 910 | new | stable discovery schema |
| `robot-docs guide --json` | absent | 900 | new | structured startup and identity guidance |
| `mail status --json` | 510 | 900 | +390 | one deterministic JSON document |
| roster projection | 560 | 900 | +340 | interpretation and recommended action |
| guard MCP descriptions | 350 | 860 | +510 | tools/list now explains mutation and use |
| runtime reconciliation description | 540 | 910 | +370 | explicitly rejects readback-only reconciliation |
| canonical usage name | 430 | 940 | +510 | launcher errors name `mcp-agent-mail` |
| launcher recovery | 300 | 890 | +590 | checked dependency and exact repair command |

Median uplift across these ten high-impact surfaces: **+510 points**.

No existing command, MCP tool schema, lifecycle state, or output default was removed. The intentional lifecycle `route_missing` state remains unchanged.
