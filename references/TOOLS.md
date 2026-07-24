# Agent Mail Tools Reference

## Table of Contents
- [Project & Identity](#project--identity)
- [Messaging](#messaging)
- [File Reservations](#file-reservations)
- [Contact Management](#contact-management)
- [Macros](#macros)
- [Guard Tools](#guard-tools)
- [Health](#health)

---

## Project & Identity

### ensure_project

Create/ensure project exists.

```
ensure_project(human_key="/abs/path/to/project")
```

**Returns:** `{id, slug, human_key, created_at}`

In core mode this is an owner-authorized control-plane operation and requires
`owner_token`. Ordinary panes should receive an already ensured project and a
short-lived registration bootstrap from the controller.

### register_agent

Register identity in project.

```
register_agent(
  project_key="/abs/path/project",
  program="claude-code",
  model="YOUR_MODEL",
  name="GreenCastle",
  task_description="Auth work",
  bootstrap_credential=<secure bootstrap>,
  idempotency_key="register:GreenCastle:attempt-1"
)
```

**Agent naming rules:**
- Adjective+noun is accepted: GreenCastle, BlueLake, RedBear
- Stable explicit IDs matching `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` are also
  accepted: `core-1`, `cc-0`, `worker_42`
- Descriptive role names may be rejected by stricter deployment policy
- Core mode requires an explicit stable name
- Auto-generation is legacy/migration behavior only

Core registration also requires a request-scoped pane/window UUID matching the
bootstrap and returns `registration_token` plus `pane_credential`. Both are
secrets; capture them outside prompts and logs.

### Core owner credential tools

- `issue_registration_bootstrap(project_key, window_uuid, owner_token, ttl_seconds)`
- `rotate_pane_credential(project_key, credential_id, expected_generation, owner_token)`
- `revoke_pane_credential(project_key, credential_id, owner_token, reason)`
- `reissue_pane_credential(project_key, agent_name, window_uuid, owner_token)`
- `unretire_agent(project_key, agent_name, owner_token)`; revoked pane
  credentials remain revoked and must be reissued

These tools belong in the trusted launcher/control plane, not ordinary agent prompts.

### whois

Get an agent profile. Commit enrichment is legacy/migration-only.

```
whois(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  include_recent_commits=false
)
```

Core mode must set `include_recent_commits=false`; the default commit-enriched
path opens the legacy archive.

### create_agent_identity (legacy/migration)

Always create new unique agent (never updates existing).

This helper writes the legacy archive and is unavailable in core mode. Core
identities use the managed `register_agent` flow.

```
create_agent_identity(
  project_key="/abs/path/project",
  program="claude-code",
  model="YOUR_MODEL",
  name_hint="GreenCastle"    # Optional
)
```

---

## Messaging

### send_message

Send message to one or more recipients.

```
send_message(
  project_key="/abs/path/project",
  sender_name="GreenCastle",
  to=["BlueLake"],
  subject="API review needed",
  body_md="Please check the auth endpoints...",
  cc=["RedBear"],            # Optional
  bcc=["Overseer"],          # Optional
  thread_id="bd-123",        # Optional, for threading
  importance="normal",       # low|normal|high|urgent
  ack_required=true,          # Request acknowledgment
  idempotency_key="send:bd-123:review-request:v1"
)
```

With a valid pane credential HTTP header, `sender_name` and secret tool
arguments may be omitted. Reuse the same idempotency key only when replaying the
same logical request after an ambiguous failure.

### reply_message

Reply preserving thread.

```
reply_message(
  project_key="/abs/path/project",
  message_id=1234,
  sender_name="BlueLake",
  body_md="Looks good, one suggestion...",
  to=["GreenCastle"],        # Optional, defaults to original sender
  cc=["RedBear"],            # Optional
  subject_prefix="Re:",      # Default
  idempotency_key="reply:1234:review-response:v1"
)
```

### fetch_inbox

Get messages for agent.

```
fetch_inbox(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  limit=20,
  since_ts="2025-01-01T00:00:00Z",  # Optional
  urgent_only=false,
  include_bodies=true
)
```

### mark_message_read

Mark message as read.

```
mark_message_read(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  message_id=1234
)
```

### acknowledge_message

Acknowledge receipt (also marks read).

```
acknowledge_message(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  message_id=1234
)
```

### search_messages

FTS5 full-text search.

```
search_messages(
  project_key="/abs/path/project",
  query='"auth module" AND error',
  limit=20
)
```

### summarize_thread

Extract key points and actions.

```
summarize_thread(
  project_key="/abs/path/project",
  thread_id="bd-123",
  include_examples=true,
  llm_mode=true
)
```

---

## File Reservations

### file_reservation_paths

Reserve files before editing.

```
file_reservation_paths(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  paths=["src/auth/**/*.ts", "src/middleware/auth.ts"],
  ttl_seconds=3600,
  exclusive=true,
  reason="bd-123",
  idempotency_key="reserve:bd-123:auth-files:v1"
)
```

**Returns:** `{granted: [...], conflicts: [...]}`

Conflicts are advisory — reservations still granted.

### release_file_reservations

Release reservations.

```
release_file_reservations(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  paths=["src/auth/**"],     # Optional, releases all if omitted
  file_reservation_ids=[101], # Optional, by ID
  idempotency_key="release:bd-123:auth-files:v1"
)
```

### renew_file_reservations

Extend TTL.

```
renew_file_reservations(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  extend_seconds=1800,
  idempotency_key="renew:bd-123:auth-files:v1"
)
```

### force_release_file_reservation (legacy/migration)

Clear stale reservation from another agent.

This helper is not part of the reviewed core idempotent lifecycle contract.

```
force_release_file_reservation(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  file_reservation_id=101,
  note="Agent crashed, clearing stale lock",
  notify_previous=true
)
```

---

## Contact Management

Contact mutation tools are legacy/migration-only in the current build. They do
not yet implement the managed core route/audit/idempotency contract, and
`register_if_missing=true` can create an identity outside managed core
registration. Do not use them for core projects.

### request_contact

Request permission to message another agent.

```
request_contact(
  project_key="/abs/path/project",
  from_agent="GreenCastle",
  to_agent="BlueLake",
  to_project="/abs/path/other",  # Optional, for cross-project
  reason="API coordination",
  ttl_seconds=604800             # 7 days default
)
```

### respond_contact

Accept or deny contact request.

```
respond_contact(
  project_key="/abs/path/project",
  to_agent="BlueLake",
  from_agent="GreenCastle",
  accept=true
)
```

### list_contacts

List contact links for agent.

```
list_contacts(
  project_key="/abs/path/project",
  agent_name="GreenCastle"
)
```

### set_contact_policy

Set contact policy.

```
set_contact_policy(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  policy="auto"  # open|auto|contacts_only|block_all
)
```

---

## Macros (legacy/migration)

These macros predate the managed core bootstrap and do not consistently expose
the idempotency inputs required for safe core retries. Use direct core tools
unless a deployment explicitly ships upgraded macro contracts.

### macro_start_session (legacy/migration)

One-call bootstrap.

```
macro_start_session(
  human_key="/abs/path/project",
  program="claude-code",
  model="YOUR_MODEL",
  task_description="Auth refactor",
  file_reservation_paths=["src/auth/**"],
  inbox_limit=10
)
```

**Returns:** `{project, agent, file_reservations, inbox}`

Do not use this macro for first registration in core mode. It does not accept
the managed bootstrap/idempotency inputs required by core registration.

### macro_prepare_thread

Join existing thread with context.

```
macro_prepare_thread(
  project_key="/abs/path/project",
  thread_id="bd-123",
  program="claude-code",
  model="YOUR_MODEL",
  include_examples=true,
  inbox_limit=10
)
```

### macro_file_reservation_cycle

Reserve, work, auto-release.

```
macro_file_reservation_cycle(
  project_key="/abs/path/project",
  agent_name="GreenCastle",
  paths=["src/auth/**"],
  ttl_seconds=3600,
  auto_release=true
)
```

### macro_contact_handshake

Contact setup with optional auto-accept.

```
macro_contact_handshake(
  project_key="/abs/path/project",
  requester="GreenCastle",
  target="BlueLake",
  auto_accept=true,
  welcome_subject="Coordination request",
  welcome_body="Let's sync on API changes"
)
```

---

## Guard Tools

### install_precommit_guard

Install git pre-commit hook to enforce file reservations.

```
install_precommit_guard(
  project_key="/abs/path/project",
  code_repo_path="/abs/path/project"
)
```

### uninstall_precommit_guard

Remove pre-commit guard.

```
uninstall_precommit_guard(code_repo_path="/abs/path/project")
```

---

## Health

### health_check

Return a configuration/health echo from the MCP tool. This is not the HTTP
readiness probe.

```
health_check()
```

**Returns:** `{status: "ok", environment, http_host, http_port, database_url}`,
with any URL password redacted.

Treat the remaining database host/path as operationally sensitive topology.
Record only sanitized status fields in chat, logs, tickets, or bead notes.

For HTTP deployment checks use both `/health/liveness` and
`/health/readiness`; a live process may still be unready.
