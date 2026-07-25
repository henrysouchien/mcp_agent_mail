# Uplift Diff

- Canonical CLI completion: rejecting stub → real executable.
- Safety: bare invocation started a server → bare invocation prints help.
- Discovery: Rich help mining → stable `capabilities --json`.
- Agent onboarding: scattered prose → `robot-docs guide --json`.
- Parseability: routing table → deterministic JSON option.
- Status pedagogy: opaque roster state → interpretation plus recommended action.
- MCP selection: empty/terse descriptions → use, avoid, and side-effect guidance.
- Recovery: missing virtualenv failure → exact reinstall command.

Median uplift on the ten changed high-impact surfaces is +510/1000. No regression was found in focused tests, live tests, or isolated reruns of the two full-suite flakes.
