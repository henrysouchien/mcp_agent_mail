# Core Deployment and Credential Lifecycle

Use this reference for the Git-independent Agent Mail runtime (`RUNTIME_PROFILE=core`). This is an operator and launcher contract; ordinary agents must not receive owner authority or credential peppers.

## Required Server Configuration

Configure these through the deployment's secret manager or service environment:

- `RUNTIME_PROFILE=core`
- `CORE_OWNER_TOKEN`: high-entropy owner bearer used only by trusted control-plane operations
- `CREDENTIAL_PEPPERS_JSON`: JSON object mapping key IDs to base64url-encoded peppers of at least 32 bytes
- `CREDENTIAL_CURRENT_PEPPER_KEY_ID`: a key present in `CREDENTIAL_PEPPERS_JSON`
- `BLOB_STORAGE_ROOT`: durable storage outside the legacy Git archive
- `DATABASE_URL`: durable database location

Never place these values in a skill, prompt, repository, tmux option, command transcript, or Agent Mail message. Keep old peppers available while credentials minted under them remain valid; rotate by adding a new key, switching the current key ID, rotating/reissuing credentials, then retiring the old key only after verification.

Core startup fails closed when required owner/pepper configuration is absent or when existing projects have not been cut over to the Git-independent route. Do not change the live service to core merely by setting an environment variable; migrate and verify project routing first.

## Health and Rollout

Check both endpoints:

```bash
curl -fsS http://127.0.0.1:8765/health/liveness
curl -fsS http://127.0.0.1:8765/health/readiness
```

Liveness means the process is serving. Readiness means startup/storage checks passed. During rollout, verify the running process was started from the intended build; a healthy old process has not loaded newly installed code.

Use a coordinated service-manager restart after configuration and migration checks. Do not kill a shared daemon ad hoc while agents have in-flight writes.

## Managed Registration

The trusted controller performs:

```text
ensure_project(human_key=<absolute project key>, owner_token=<owner bearer>)

issue_registration_bootstrap(
  project_key=<absolute project key>,
  window_uuid=<canonical pane UUID>,
  owner_token=<owner bearer>,
  ttl_seconds=600
)
```

The pane then performs exactly one logical registration, replaying the same request and key after ambiguous failures:

```text
register_agent(
  project_key=<absolute project key>,
  program=<client>,
  model=<model>,
  name=<explicit stable agent name>,
  task_description=<task>,
  bootstrap_credential=<single-use bootstrap bearer>,
  idempotency_key=<stable registration operation key>
)
```

The request must carry the same pane UUID used to mint the bootstrap. The response includes both a recovery-oriented `registration_token` and the normal `pane_credential`. Capture secrets programmatically; do not ask an agent to quote them back.

## Durable Pane Binding

There are three distinct values:

| Value | Secret? | Purpose |
|---|---:|---|
| `agent_name` | No | Human-readable routing identity |
| pane/window UUID | No | Stable scope used during registration and local routing |
| `pane_credential` | Yes | Durable sender authority across stateless HTTP requests/reconnects |

For HTTP, inject the pane bearer as `X-MCP-Agent-Mail-Pane-Credential`; `X-Agent-Mail-Pane-Credential` is an accepted alias. Never put it in tool arguments when the client can supply the header.

The window UUID is not sender authority. Prefer
`X-MCP-Agent-Mail-Window-ID`. The `mcp-window:<uuid>` Authorization syntax is a
compatibility transport only where Authorization is not already occupied by
HTTP bearer/JWT authentication; it does not authenticate an Agent Mail
identity. Only the pane credential (or an explicitly accepted recovery
credential) authorizes stateless writes.

The launcher must persist the bearer in an OS-protected secret store keyed by deployment, project, and pane/window UUID. A tmux `@am_agent_name` binding is only routing metadata. If the launcher preserves only the name and UUID, a reconnect will still fail authentication.

An invalid explicit pane bearer fails closed and must not silently fall back to session state. Deregistering or retiring an identity revokes its pane credentials. Restoring the identity does not revive them.

## Recovery and Rotation

Owner-only tools:

- `rotate_pane_credential(project_key, credential_id, expected_generation, owner_token)` rotates with generation fencing.
- `revoke_pane_credential(project_key, credential_id, owner_token, reason)` invalidates immediately.
- `reissue_pane_credential(project_key, agent_name, window_uuid, owner_token)` recovers a lost bearer and normally revokes existing credentials for that pane.
- `unretire_agent(project_key, agent_name, owner_token)` restores a retired identity without reviving revoked pane credentials.

Handle returned bearers only in the trusted launcher/control plane. Update the secret store atomically before restarting the MCP client. A retired identity must first be restored with owner-authorized `unretire_agent`; then reissue a new pane credential because old credentials remain revoked.

## Retry Contract

Every retryable core mutation that exposes `idempotency_key` gets one stable
key. Keep it stable across network/client timeouts and keep the logical request
unchanged. Direct mark/ack is naturally idempotent; owner credential lifecycle
uses owner authority and, where documented, generation fencing. Expected
structured failures for keyed operations include:

- key already used for different request content: do not retry with modified content under that key;
- receipt expired: the key remains reserved, so reconcile state and deliberately choose a new logical operation if needed;
- receipt/version unavailable: stop automatic retries and escalate for compatibility review.

This is what removes the old "the server may have committed 0, 1, or 2 times" ambiguity. Retrying with a fresh key defeats the protection.

The current contact-management mutations and `update_messages` batch helper
have not yet been upgraded to this core contract. Keep contact workflows in
legacy/migration mode, and use the direct `mark_message_read` and
`acknowledge_message` tools for core read/ack mutations.

## Git Boundary

Core Agent Mail persistence is database/blob based and must start and serve without GitPython or a `git` executable. User code repositories remain ordinary Git repositories. The optional legacy archive, archive CLI, Git-derived project identity, and pre-commit reservation guard are separate compatibility/operator features and must not be described as core message durability.
