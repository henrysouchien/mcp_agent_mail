---
name: agent-mail
description: >-
  MCP Agent Mail for multi-agent coordination. Use when agents need file locks,
  messaging, inboxes, or conflict prevention. Handles macro_start_session,
  file_reservation_paths, send_message, threading, pre-commit guards.
---

<!-- TOC: Deployment Mode | Bootstrap | Core Ops | File Reservations | Beads | Troubleshooting | Identity | Human Overseer | Pre-Commit Guard | References -->

# Using MCP Agent Mail

> **Core Insight:** Without coordination, multiple agents overwrite each other's work. Agent Mail provides identities, messaging, and file reservations to prevent conflicts.

## When to Use What

| Situation | Action |
|-----------|--------|
| Starting in legacy/migration mode | `macro_start_session` |
| Starting in core mode | Use the managed registration flow below; do not use `macro_start_session` for first registration |
| About to edit files | `file_reservation_paths` → edit → `release_file_reservations` |
| Need to tell another agent something | `send_message` with `thread_id` and a stable `idempotency_key` |
| Mail notification or phase boundary | Core: `sync_inbox` → `read_messages` → `mark_message_read`/`acknowledge_message`; legacy/migration may batch with `update_messages` |
| Picking up someone else's work | Core: read the thread/inbox directly; legacy/migration: `macro_prepare_thread` |
| Can't message an agent | Core: stop and coordinate out of band; contact mutation tools remain legacy/migration |
| Server seems broken | Check liveness and readiness separately, then use `health_check()`; do not retry a timed-out write with a new idempotency key |

---

## Deployment Mode

Agent Mail has two operational contracts:

- **Core (`RUNTIME_PROFILE=core`)**: SQLite plus content-addressed blobs are authoritative. Runtime messaging does not require Git. Registration is managed, names are explicit, retryable mutations use their documented idempotency/fencing contract, and durable pane authentication uses an opaque pane bearer.
- **Legacy/migration**: the Git archive remains available and the historical macros and auto-registration behavior still apply.

Do not infer the mode from the fact that the collaborating code repository uses Git. "Git-independent" means Agent Mail's own runtime persistence no longer shells out to Git; it does not mean user repositories stop using Git.

For rollout and operator requirements, read [CORE-DEPLOYMENT.md](references/CORE-DEPLOYMENT.md).

---

## Session Bootstrap

### Core mode (new deployment)

First registration is a controller-mediated flow:

1. The launcher supplies a stable, pane-scoped UUID as `X-MCP-Agent-Mail-Window-ID`. A compatibility `Authorization: Bearer mcp-window:<uuid>` routing carrier exists only for deployments that do not already use Authorization for HTTP bearer/JWT authentication. The UUID scopes registration but does not authenticate the pane.
2. An owner-authorized controller ensures the project and issues a short-lived bootstrap credential for that UUID.
3. Call `register_agent` with an explicit stable `name`, the bootstrap credential, and one stable `idempotency_key`.
4. Securely persist the returned `pane_credential` outside prompts, logs, tmux options, and repository files.
5. Inject it on later HTTP requests as `X-MCP-Agent-Mail-Pane-Credential`. The alias `X-Agent-Mail-Pane-Credential` is also accepted.

```text
register_agent(
  project_key="/abs/path/to/project",
  program="claude-code",
  model="YOUR_MODEL",
  name="GreenCastle",
  task_description="Working on auth module",
  bootstrap_credential=<securely supplied bootstrap>,
  idempotency_key="register:GreenCastle:attempt-1"
)
```

Never print, paste into chat, or commit `CORE_OWNER_TOKEN`, bootstrap credentials, registration tokens, pane credentials, or credential peppers. On a timeout, replay the exact request with the exact same idempotency key.

`macro_start_session` does not yet implement the managed first-registration contract. Use it only in legacy/migration mode or for a deployment that explicitly wraps it with managed registration.

### Legacy/migration mode

```
macro_start_session(
  human_key="/abs/path/to/project",
  program="claude-code",
  model="YOUR_MODEL",
  task_description="Working on auth module"
)
```

Returns: `{project, agent, file_reservations, inbox}`

This single call: ensures project exists → registers your identity → fetches inbox.

If the agent is running in tmux, immediately bind the returned Agent Mail identity to the pane:

```bash
tmux-agent-bind-mail \
  --pane "$TMUX_PANE" \
  --project "/abs/path/to/project" \
  --agent "<agent.name from macro_start_session>" \
  --agent-id "<agent.id from macro_start_session>" \
  --program "claude-code" \
  --model "YOUR_MODEL" \
  --task "Working on auth module" \
  --source macro_start_session \
  --verified
```

This lets `tmux-agent-panes`, `tmux-agent-who`, and `tmux-agent-send --to-agent ...` map Agent Mail
names to live tmux panes. This local routing record is not authentication. In core mode, only the
server-issued pane credential (or an explicitly accepted recovery credential) grants durable sender
authority across MCP reconnects.

---

## Core Operations

| Task | Tool |
|------|------|
| Bootstrap legacy session | `macro_start_session(human_key, program, model, task_description)` |
| Register core session | controller bootstrap → `register_agent(..., name, bootstrap_credential, idempotency_key)` |
| Stage a fleet launch | `ensure_fleet_principal(...)` with owner authority, expected generation, supervisor sequence, and stable launch/idempotency ids |
| Activate a fleet runtime | `activate_fleet_runtime(...)` with the Phase-A receipt, exact process/route facts, recovery authority, and the next supervisor sequence |
| Continue the same provider session | Current runtime calls `issue_continuation_receipt(...)`; launcher uses `continue_with_receipt` through ensure/activate |
| Publish fleet readiness | `publish_fleet_runtime_observation(...)` after exact route readback and identity-context injection |
| Project exhausted launch failure | `publish_fleet_launch_state(..., coordination_state="failed")`; supports a nullable-principal pre-Phase-A row |
| End an exactly absent stale runtime | `end_fleet_runtime_absent(...)` with two fresh absence events and exact runtime fences |
| Send message | `send_message(project_key, to, subject, body_md, idempotency_key)`; identity may come from pane bearer |
| Reply in thread | `reply_message(project_key, message_id, body_md, idempotency_key)`; identity may come from pane bearer |
| Synchronize inbox | `sync_inbox(project_key, agent_name, cursor)` |
| Read selected bodies | `read_messages(project_key, agent_name, message_ids)` |
| Mark read / acknowledge in core | `mark_message_read(...)` / `acknowledge_message(...)` |
| Batch read / acknowledge (legacy/migration) | `update_messages(project_key, agent_name, read_ids, acknowledge_ids)` |
| Reserve files | `file_reservation_paths(project_key, agent_name, paths, ttl_seconds, idempotency_key)` |
| Release files | `release_file_reservations(project_key, agent_name, idempotency_key)` |
| Search messages | `search_messages(project_key, "query")` |

### Core Write Rule

Use a stable, unique `idempotency_key` for each logical send, reply, reservation acquire/renew/release, and every other retryable core mutation that exposes that parameter. If the client times out, retry the byte-for-byte equivalent logical request with the same key. Never mint a new key merely because the response was lost; that converts an ambiguous success into a possible duplicate. Direct mark/ack operations are naturally idempotent; owner credential lifecycle operations use owner authority and, where documented, generation fencing.

In stateless HTTP core deployments, prefer header-based pane authentication and omit secret tool arguments. Compatibility aliases `agent_name` and `registration_token` still exist for send/reply recovery paths, but they are not the normal durable-pane transport.

### Managed Fleet Lifecycle

Use the fleet contract for tmux-supervised Codex, Claude, and Grok runtimes:

1. Stage with `ensure_fleet_principal`. For every non-create proof, supply the exact active `expected_generation`; a stale value must fail before launch state or authority is changed.
2. Launch or resume the provider process.
3. Activate with `activate_fleet_runtime` using the Phase-A receipt and exact host, boot, tmux, pane, process, and provider-session facts.
4. Read the route back through the target pane carrier, inject identity context, then publish readiness with `publish_fleet_runtime_observation`.
5. If provider launch/proof fails after staging, sequence-advance desired state
   and call `publish_fleet_launch_state(..., coordination_state="failed",
   identity_context_injected=false)`. Replay it through the supervisor journal.

The final `ready` observation reconciles the durable principal's program,
model, task description, and activity timestamp from the exact active runtime
binding. Do not issue a separate token-backed `register_agent` profile refresh
after a successful managed launch. A later exact healthy heartbeat repairs any
pre-cutover profile drift from the same binding. Non-ready, unhealthy, or stale
generations cannot rewrite the principal profile.

An exhausted failure before principal creation uses the same terminal
projection. It may create the normalized project and a nullable-principal
`launch_failed` roster row. Its higher supervisor sequence fences any delayed
older Phase-A replay. If the projection committed but its response was lost,
replay the identical supervisor sequence and full state payload; exact replay
succeeds, while any changed same-sequence payload remains fenced.

Do not end stale runtimes from time or heartbeat age alone. The controller must
call `end_fleet_runtime_absent` with two distinct, current-generation events:
fresh absence of the immutable pane instance and fresh absence of the exact
provider PID/process-start tuple. Both must be no older than 45 seconds and the
last healthy observation must be at least five minutes old. Success ends only
that runtime generation and leaves the durable principal as `durable_only`.
The supervisor must journal the runtime incarnation and pane instance from the
pane event plus the PID/start tuple from the process event; the adapter checks
those facts against persisted Phase-B state before the server mutation.
Observer outage, unreachable inventory, one-sided absence, or changed identity
fails closed.

In the installed fleet, the periodic `fleet-observer` is the normal producer
of those proofs. It performs one shared tmux→libproc→tmux census, requires the
exact `cx`, `clx`, or `gx` registry entry before publishing health, records the
two absence rows atomically after the grace period, and delegates retirement
through the supervisor journal. Treat `operational=false`, degraded/old
status, registry mismatch, or one-sided absence as unavailable evidence; do
not hand-create proof rows or infer runtime end from heartbeat age.

For an exact-session respawn, issue a short-lived receipt from the authoritative runtime, then use `proof_mode=continue_with_receipt` for both phases. Preserve `runtime_session_id`, advance runtime generation, and use a fresh pane carrier. The agent profile intentionally exposes `issue_continuation_receipt` but not direct `consume_continuation_receipt`; activation performs single-use consumption atomically. A consumed, expired, differently reserved, or stale-generation receipt must fail without moving authority.

Use `rotate_or_takeover` only with an explicit reason. Use `resume_same_authority` only when the current locator remains authoritative. Replay an exact successful request with its original idempotency key; changed payloads require a new logical operation and key.

In the installed fleet, `cx`, `clx`, `gx`, and `fleet-supervisor` are the normal
callers of this contract. An agent launched through those wrappers should
already have model-visible public identity context and authenticated pane
authority when its roster row becomes `live`. It must not call
`macro_start_session`, choose another name, or manually bind a pane as part of
normal startup.

The fleet launch wrapper consumes controller/registration authority before
provider exec. Provider environments retain the pane-scoped HTTP window bearer
but must not retain `CORE_OWNER_TOKEN`, Agent Mail owner-token variables, or
Agent Mail registration-token variables.

The supervisor owns desired state, launch attempts, sequence ordering, and
replay. Agent Mail owns the durable principal, current authority, runtime CAS,
roster projection, and messages. Tmux binding metadata is public route
evidence only.

### Token-Efficient Inbox Protocol

1. Keep the last `next_cursor` in session/task state.
2. Call `sync_inbox` only after a notification or at a meaningful workflow boundary, not after every edit.
3. Inspect metadata, then call `read_messages` once with only the message ids whose bodies are needed.
4. In core mode, call `mark_message_read` or `acknowledge_message` for the selected ids. Use `update_messages` batching only in legacy/migration mode until that path is upgraded to the core audit/route contract.
5. Continue using `send_message` and `reply_message`; their receipts do not echo the authored body.

Use legacy `fetch_inbox` only when cursor sync is unavailable or a specialized recent-history query is required. Do not repeatedly fetch full bodies without `since_ts`.

### The Four Legacy/Migration Macros

The current macros predate managed core registration and do not consistently
carry core idempotency inputs. Do not use them as core-mode mutation wrappers
unless the deployment has explicitly upgraded their contracts.

| Macro | When to Use |
|-------|-------------|
| `macro_start_session` | Bootstrap: project → agent → inbox |
| `macro_prepare_thread` | Join existing thread with summary |
| `macro_file_reservation_cycle` | Reserve → work → auto-release |
| `macro_contact_handshake` | Cross-agent contact setup (legacy/migration only) |

### Fast Resource Reads (No Tool Call Required)

| Need | Resource |
|------|----------|
| List agents | `resource://agents/{project_key}` |
| Inbox | `resource://inbox/{agent}?project=/abs/path&limit=20` |
| Thread | `resource://thread/{thread_id}?project=/abs/path&include_bodies=true` |
| Ack-required | `resource://views/ack-required/{agent}?project=/abs/path` |

---

## File Reservations

### Reserve Before Editing

```
file_reservation_paths(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  paths=["src/auth/**/*.ts"],
  ttl_seconds=3600,
  exclusive=true,
  reason="bd-123",
  idempotency_key="reserve:bd-123:auth-files:v1"
)
```

Returns: `{granted: [...], conflicts: [...]}`

### Conflict Resolution

If conflicts exist:
1. **Wait** — TTL will expire
2. **Coordinate** — Message the holder
3. **Share** — Use `exclusive=false`

### Release When Done

```
release_file_reservations(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  idempotency_key="release:bd-123:auth-files:v1"
)
```

---

## Beads Integration

Use bead IDs as your threading anchor:

```
1. Pick work:        br ready --json → choose bd-123
2. Reserve files:    file_reservation_paths(..., reason="bd-123", idempotency_key="reserve:bd-123:v1")
3. Announce:         send_message(..., thread_id="bd-123", subject="[bd-123] Starting...", idempotency_key="send:bd-123:start:v1")
4. Work:             Reply in thread with progress
5. Complete:         br close bd-123, release_file_reservations(..., idempotency_key="release:bd-123:v1"), final keyed message
```

**Bead ID (often bd-###) goes in:** thread_id, subject prefix, reservation reason, commit message

---

## Quick Troubleshooting

| Error | Fix |
|-------|-----|
| `MANAGED_REGISTRATION_REQUIRED` | Core mode needs a controller-issued bootstrap credential plus an idempotency key |
| `PANE_IDENTITY_REQUIRED` | Relaunch with a valid pane-scoped window UUID carrier before registration |
| `AUTHENTICATION_REQUIRED` | Restore the pane credential header; owner recovery may reissue a bearer, but UUID/name metadata alone is not authority |
| "sender_name not registered" | In core mode use managed registration; in legacy/migration mode call `macro_start_session` |
| Timed-out write | Retry the same logical request with the same idempotency key; check the inbox/receipt before creating a new operation |
| Idempotency mismatch/expired receipt | Do not reuse the key for different content; preserve the original request or start a deliberately new logical operation |
| "FILE_RESERVATION_CONFLICT" | Wait, coordinate, or use `exclusive=false` |
| "CONTACT_BLOCKED" | Core: stop and coordinate out of band; `request_contact` and contact macros are not yet core-safe |
| Empty inbox | Check `cursor`, `since_ts`, `urgent_only`, and agent name spelling |
| Server unreachable | Check `/health/liveness`, then `/health/readiness`; liveness alone does not authorize writes |
| Claude lacks Agent Mail tools | Check `claude mcp get mcp-agent-mail`. Current Claude Code reads user-scope MCP servers from root `~/.claude.json`; a legacy `~/.claude/settings.json` `mcpServers` block alone is not enough. Re-add with `claude mcp add-json --scope user mcp-agent-mail ...`, then restart/resume the Claude pane if tools are still missing. |
| Guard blocks commit | Set `AGENT_NAME` env var; bypass: `AGENT_MAIL_BYPASS=1 git commit` |

### HTTP Diagnostics

```bash
# Process is serving HTTP
curl -fsS http://127.0.0.1:8765/health/liveness

# Storage and startup state are ready
curl -fsS http://127.0.0.1:8765/health/readiness
```

### Legacy/Migration Doctor Diagnostics (CLI-only, optional)

The current doctor/repair CLI imports legacy storage and may require the
optional Git dependency. Do not run it against a core deployment. For core,
use liveness/readiness plus database audit-ledger and blob-store diagnostics
from the deployment runbook.

```bash
# Full diagnostics (CLI)
uv run python -m mcp_agent_mail.cli doctor check --verbose

# Preview repairs (dry run, CLI)
uv run python -m mcp_agent_mail.cli doctor repair --dry-run

# Apply repairs (CLI)
uv run python -m mcp_agent_mail.cli doctor repair --yes
```

---

## Agent Identity

Agents may use adjective+noun names such as GreenCastle, BlueLake, and RedBear,
or stable explicit IDs matching `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` such as
`core-1` or `worker_42`. Descriptive role names may still be rejected by a
deployment's stricter naming policy.

In core mode, an explicit stable name is required. Auto-generation is legacy/migration behavior only.

```
register_agent(
  project_key="/abs/path/project",
  program="claude-code",
  model="YOUR_MODEL",
  name="GreenCastle",
  task_description="Auth refactor",
  bootstrap_credential=<securely supplied bootstrap>,
  idempotency_key="register:GreenCastle:attempt-1"
)
```

`agent_name` is a public identifier. `registration_token` and `pane_credential` are secrets. A tmux pane label containing the agent name is useful for routing but cannot replace the credential.

---

## Human Overseer (Legacy/Migration Only)

The current web compose/send route does not implement the core atomic,
idempotent audit contract and is rejected before mutation in core mode. Do not
use it for core projects.

Send urgent messages to agents from the web UI at `http://127.0.0.1:8765/mail`:

1. Click "Human Overseer" mode
2. Compose with `importance: urgent`
3. Select target agents

Agents see urgent messages via `fetch_inbox(..., urgent_only=true)`.

---

## Pre-Commit Guard

```
install_precommit_guard(project_key="/abs/path", code_repo_path="/abs/path")
```

- Set `AGENT_NAME` env var so guard knows who you are
- Bypass emergency: `AGENT_MAIL_BYPASS=1 git commit -m "fix"`
- Warning mode: `AGENT_MAIL_GUARD_MODE=warn`

---

## Search Syntax (FTS5)

```
"exact phrase"
prefix*
term1 AND term2
term1 OR term2
(auth OR login) AND NOT admin
```

---

## References

| Topic | Reference |
|-------|-----------|
| All MCP tools | [TOOLS.md](references/TOOLS.md) |
| Workflow patterns | [WORKFLOWS.md](references/WORKFLOWS.md) |
| MCP resources | [RESOURCES.md](references/RESOURCES.md) |
| Cross-project setup | [CROSS-PROJECT.md](references/CROSS-PROJECT.md) |
| Doctor & recovery | [RECOVERY.md](references/RECOVERY.md) |
| Installation | [INSTALL.md](references/INSTALL.md) |
| Fix MCP config | [FIX-MCP-CONFIG.md](references/FIX-MCP-CONFIG.md) |
| Core deployment and credential lifecycle | [CORE-DEPLOYMENT.md](references/CORE-DEPLOYMENT.md) |
| Product bus, build slots, internals | [ADVANCED.md](references/ADVANCED.md) |

---

## Validation

```bash
# Server health
curl -fsS http://127.0.0.1:8765/health/liveness
# → {"status":"alive"}
curl -fsS http://127.0.0.1:8765/health/readiness
# → {"status":"ready"}

# Start server if needed
am
```
