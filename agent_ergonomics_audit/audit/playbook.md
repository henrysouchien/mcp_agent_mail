# Agent Ergonomics Improvement Playbook

Apply the seven recommendations in priority order, preserving one canonical CLI and the existing MCP identity lifecycle contract.

1. Cut the installed false stub over to the real Typer CLI.
2. Make no-argument invocation show help; daemon startup remains `serve-http`.
3. Add stable discovery through `capabilities --json` and `robot-docs guide`.
4. Give `mail status` a JSON renderer.
5. Add structured meaning and next action to every roster state.
6. Expand MCP tool-selection documentation for guard and runtime identity operations.
7. Pin each behavior in source tests and audit regression tests, then rerun the intent corpus and rescore.

Do not rename lifecycle `route_missing`: downstream launch/reconcile clients consume it as a convergence state. Query-only `identity_status` keeps the separate `verification_not_requested` contract when the caller omits route evidence.
