# Agent Mail Workflow Patterns

## Table of Contents
- [Standard Bead Workflow](#standard-bead-workflow)
- [Collaborative Review](#collaborative-review)
- [Context Handoff](#context-handoff)
- [Conflict Resolution](#conflict-resolution)
- [Daily Standup Pattern](#daily-standup-pattern)

---

## Standard Bead Workflow

The canonical workflow for working on a bead with coordination.

### Steps

```
1. Bootstrap session
   - Core: use the controller-issued managed registration flow in
     `CORE-DEPLOYMENT.md`; preserve the returned pane bearer in the launcher.
   - Legacy/migration: `macro_start_session(human_key="/abs/path", program="claude-code", model="YOUR_MODEL")`

2. Pick work
   br ready --json → select bd-123

3. Reserve files
   file_reservation_paths(
     project_key="/abs/path",
     agent_name="GreenCastle",
     paths=["src/auth/**/*.ts"],
     reason="bd-123",
     idempotency_key="reserve:bd-123:auth-files:v1"
   )

4. Announce start
   send_message(
     project_key="/abs/path",
     sender_name="GreenCastle",
     to=["BlueLake", "RedBear"],  # Other active agents
     subject="[bd-123] Starting auth refactor",
     body_md="Reserving src/auth/**. Expected 2 hours.",
     thread_id="bd-123",
     ack_required=true,
     idempotency_key="send:bd-123:start:v1"
   )

5. Work on bead
   - Make changes
   - Periodically check inbox
   - Reply in thread with progress updates

6. Complete
   br close bd-123 --reason "Implemented OAuth flow"
   release_file_reservations(
     project_key="/abs/path",
     agent_name="GreenCastle",
     idempotency_key="release:bd-123:auth-files:v1"
   )
   send_message(
     ...
     subject="[bd-123] Completed",
     body_md="OAuth flow implemented. Ready for review.",
     idempotency_key="send:bd-123:complete:v1"
   )
```

---

## Collaborative Review

When you need another agent to review your work.

### Requester

```
# After completing work
send_message(
  project_key="/abs/path",
  sender_name="GreenCastle",
  to=["BlueLake"],
  subject="[bd-123] Review request: Auth module",
  body_md="""
## What changed
- Implemented OAuth 2.0 flow
- Added token refresh logic
- Updated middleware

## Files to review
- `src/auth/oauth.ts`
- `src/middleware/auth.ts`

## Testing
- Run `npm test -- --grep auth`
""",
  thread_id="bd-123",
  importance="high",
  ack_required=true,
  idempotency_key="send:bd-123:review-request:v1"
)
```

### Reviewer

```
# Acknowledge receipt
acknowledge_message(project_key="/abs/path", agent_name="BlueLake", message_id=1234)

# After review
reply_message(
  project_key="/abs/path",
  message_id=1234,
  sender_name="BlueLake",
  body_md="""
## Review complete

**Approved** with minor suggestions:

1. Line 45: Consider using early return pattern
2. Line 78: Add error handling for token expiry

Tests pass. Good to merge.
""",
  idempotency_key="reply:1234:review-complete:v1"
)
```

---

## Context Handoff

When you need to hand off work to another agent.

### Outgoing Agent

```
# Before losing context or ending session
send_message(
  project_key="/abs/path",
  sender_name="GreenCastle",
  to=["BlueLake"],
  subject="[bd-123] Handoff: Auth module",
  body_md="""
## Current state
- OAuth flow 80% complete
- Token storage implemented
- Refresh logic TODO

## What's left
1. Implement token refresh (see `src/auth/refresh.ts`)
2. Add error handling for expired tokens
3. Update tests

## Key decisions made
- Using JWT for tokens (not opaque)
- 15-minute access token lifetime
- Refresh via httpOnly cookie

## Files touched
- `src/auth/oauth.ts` (main flow)
- `src/auth/storage.ts` (token storage)
- `src/middleware/auth.ts` (middleware)

## Context I had
- User stories in bd-120, bd-121
- API spec in docs/auth-api.md
""",
  thread_id="bd-123",
  importance="high",
  ack_required=true,
  idempotency_key="send:bd-123:handoff:v1"
)

# Transfer file reservations explicitly or let them expire
release_file_reservations(
  project_key="/abs/path",
  agent_name="GreenCastle",
  idempotency_key="release:bd-123:handoff:v1"
)
```

### Incoming Agent

```
# Core: read resource://thread/bd-123?project=/abs/path&include_bodies=true
# Legacy/migration may use macro_prepare_thread(...).

# Claim reservations
file_reservation_paths(
  project_key="/abs/path",
  agent_name="BlueLake",
  paths=["src/auth/**"],
  reason="bd-123 handoff",
  idempotency_key="reserve:bd-123:handoff:v1"
)

# Acknowledge
reply_message(
  project_key="/abs/path",
  message_id=1234,
  sender_name="BlueLake",
  body_md="Received handoff. Resuming from token refresh. Will update thread with progress.",
  idempotency_key="reply:1234:handoff-accepted:v1"
)
```

---

## Conflict Resolution

When file reservation conflicts occur.

### Detecting Conflict

```
file_reservation_paths(
  project_key="/abs/path",
  agent_name="GreenCastle",
  paths=["src/api/routes.ts"],
  idempotency_key="reserve:bd-123:routes:v1"
)

# Returns: {granted: [...], conflicts: [{path: "src/api/routes.ts", holders: ["BlueLake"]}]}
```

### Resolution Options

**Option A: Wait and retry**
```
# Conflict exists, wait for other agent
send_message(
  project_key="/abs/path",
  sender_name="GreenCastle",
  to=["BlueLake"],
  subject="File access: src/api/routes.ts",
  body_md="I need to modify routes.ts for bd-123. How long until you're done?",
  idempotency_key="send:bd-123:routes-coordinate:v1"
)

# Wait for reply, then retry reservation
```

**Option B: Coordinate scope**
```
send_message(
  project_key="/abs/path",
  sender_name="GreenCastle",
  to=["BlueLake"],
  subject="Coordinate: src/api/routes.ts",
  body_md="""
We both need routes.ts:
- I need to add auth routes (lines 50-100)
- What section do you need?

Can we split the file or take turns?
""",
  idempotency_key="send:bd-123:routes-scope:v1"
)
```

**Option C: Non-exclusive reservation**
```
# Both agents use shared reservation
file_reservation_paths(
  project_key="/abs/path",
  agent_name="GreenCastle",
  paths=["src/api/routes.ts"],
  exclusive=false,
  reason="shared: auth routes",
  idempotency_key="reserve:bd-123:routes-shared:v1"
)
```

---

## Daily Standup Pattern

For longer-running multi-agent projects.

### Broadcast Status

```
send_message(
  project_key="/abs/path",
  sender_name="GreenCastle",
  to=["BlueLake", "RedBear", "YellowFox"],
  subject="Standup: GreenCastle",
  body_md="""
## Yesterday
- Completed bd-123 (OAuth flow)
- Started bd-124 (Token refresh)

## Today
- Finish bd-124
- Start bd-125 (Auth middleware)

## Blockers
- Waiting on API spec update from BlueLake

## File reservations
- src/auth/** (until ~3pm)
""",
  importance="normal",
  idempotency_key="send:standup:GreenCastle:2026-07-21"
)
```

### Query Active Work

```bash
# Via NTM
ntm locks PROJECT --all-agents

# Via Agent Mail
# Use resource://file_reservations/{slug}
```
