# MCP Agent Mail Agent-Ergonomics Handoff

## Outcome

The installed command is now the real CLI, not a rejection stub. Bare invocation is safe. Agents can discover capabilities and operating rules as JSON, routing diagnostics are parseable, roster states explain themselves, and mutating/identity MCP tools provide selection guidance at `tools/list`.

## Validation

See `regression_alerts.md`. All focused, audit, lint, type, syntax, and isolated flake reruns pass.

## Preserved contracts

- `route_missing` remains the lifecycle convergence state used by launch/reconcile clients.
- `identity_status` without route evidence remains `verification_not_requested`.
- Human output remains the default; `--json` is additive.
- Server startup remains available only through explicit `serve-http`.

## Audit tooling issues

The generic audit skill’s intent scripts are not fully macOS portable:

- `generate_intent_corpus.sh` ended with `color: unbound variable`.
- `run_intent_corpus.sh` requires Bash `mapfile`, unavailable in macOS Bash 3.2.

The audit used safe argv-based fallbacks and retained the partial generated corpus for diagnosis.
