# Per-Pane Agent Identity Contract

**Status:** Canonical
**Date:** 2026-03-03

## Problem

Two competing conventions exist for mapping a tmux pane to an agent name:

1. **Claude Code:** `~/.claude/agent-mail/identity.$TMUX_PANE`
2. **NTM:** `/tmp/agent-mail-name.<md5(project)[0:12]>.<pane_id>`

Neither is multi-project-safe (Claude Code) or durable across reboots (NTM's `/tmp`). This document defines ONE canonical convention that both can adopt.

## Canonical Path

```
~/.local/state/agent-mail/identity/<project_hash>/<pane_id>
```

| Component      | Value                                                        |
|----------------|--------------------------------------------------------------|
| `project_hash` | First 12 hex chars of `SHA-256(absolute_project_path)`       |
| `pane_id`      | Raw `$TMUX_PANE` value (e.g., `%0`, `%1`, `%23`)            |

The base directory `~/.local/state/` follows the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/) for runtime state that should persist across reboots but is not essential configuration.

### Example

For project `/data/projects/mcp_agent_mail` in pane `%0`:

```
$ printf '%s' '/data/projects/mcp_agent_mail' | sha256sum | cut -c1-12
b4ddf33fdd57

$ cat ~/.local/state/agent-mail/identity/b4ddf33fdd57/%0
cc-0
1709472000
```

## File Format

Plain text, two lines:

```
<agent_name>\n
<unix_epoch_seconds>\n
```

| Line | Contents                         | Required |
|------|----------------------------------|----------|
| 1    | Agent name string (e.g., `cc-0`) | Yes      |
| 2    | Unix epoch timestamp (seconds)   | Yes      |

The timestamp on line 2 records when the identity was last written. It is used for staleness detection (see below). There is no separate `.ts` sidecar file; the timestamp is inline.

## Write Protocol

Writers MUST use atomic writes to prevent readers from seeing partial content.

1. Create the directory tree: `mkdir -p ~/.local/state/agent-mail/identity/<project_hash>/`
2. Set directory permissions: `chmod 700` on the directory tree.
3. Write content to a temporary file: `<final_path>.tmp.$$`
4. Set file permissions: `chmod 600` on the temp file.
5. Atomically rename: `mv <temp> <final_path>`

The reference implementation is `scripts/identity-write.sh`:

```sh
scripts/identity-write.sh <agent_name> [project_path] [pane_id]
```

Arguments:
- `agent_name` (required): The name string to write.
- `project_path` (optional): Absolute project path. Defaults to `$PWD`.
- `pane_id` (optional): The tmux pane. Defaults to `$TMUX_PANE`.

On success, prints the written file path to stdout.

## Resolve Protocol

Readers look up the identity file and check for staleness.

1. Compute the project hash and build the file path.
2. If the file does not exist, the pane has no identity. Exit non-zero.
3. Read line 1 (agent name) and line 2 (timestamp).
4. If the timestamp is older than the staleness threshold, treat as absent. Exit non-zero.
5. Print the agent name to stdout.

The default staleness threshold is **86400 seconds (24 hours)**. Override with the `IDENTITY_STALE_SECONDS` environment variable.

The reference implementation is `scripts/identity-resolve.sh`:

```sh
# Resolve the current pane's agent name:
scripts/identity-resolve.sh [project_path] [pane_id]

# Clean up stale files for a specific project:
scripts/identity-resolve.sh --cleanup /data/projects/mcp_agent_mail

# Clean up stale files for ALL projects:
scripts/identity-resolve.sh --cleanup
```

## Staleness and Cleanup

Identity files become stale when their embedded timestamp is older than `IDENTITY_STALE_SECONDS` (default: 86400 = 24h).

**Writers should refresh the timestamp** each time the agent session starts or performs a significant action (e.g., `register_agent`). This keeps the identity file alive for long-running sessions.

**Cleanup** is handled by `identity-resolve.sh --cleanup`, which:
- Walks all identity files under the given project hash (or all hashes).
- Removes files whose timestamp is older than the threshold.
- Removes empty hash directories.

Cleanup can be run periodically via cron, systemd timer, or at agent session start.

## Directory Permissions

| Path                                              | Mode |
|---------------------------------------------------|------|
| `~/.local/state/agent-mail/`                      | 700  |
| `~/.local/state/agent-mail/identity/`             | 700  |
| `~/.local/state/agent-mail/identity/<hash>/`      | 700  |
| `~/.local/state/agent-mail/identity/<hash>/<pane>` | 600  |

Agent names are not secrets, but the directory structure reveals which projects exist on the machine. Restrictive permissions are a defense-in-depth measure.

## Migration Notes

### Claude Code

The old convention was:
```
~/.claude/agent-mail/identity.$TMUX_PANE
```

To migrate:
1. At session start, check if the old file exists and the new file does not.
2. Read the agent name from the old file.
3. Write it to the new canonical path using `identity-write.sh`.
4. Optionally delete the old file.

The `integrate_claude_code.sh` script should be updated to write to the canonical path instead of `~/.claude/agent-mail/`.

### NTM

The old convention was:
```
/tmp/agent-mail-name.<md5(project)[0:12]>.<pane_id>
```

To migrate:
1. At session start, check if the old `/tmp` file exists and the new file does not.
2. Read the agent name from the old file.
3. Write it to the new canonical path using `identity-write.sh`.
4. Optionally delete the old file.

Note the hash algorithm change: NTM used MD5, the canonical convention uses SHA-256. Both truncate to 12 characters. The hashes will differ for the same project path. Migration must read the old file by reconstructing the old path (using MD5), not by matching hashes.

## Design Rationale

| Decision                        | Reason                                                                 |
|---------------------------------|------------------------------------------------------------------------|
| `~/.local/state/` base          | XDG-compliant, survives reboots (unlike `/tmp`), not config (unlike `~/.config/`) |
| SHA-256 hash                    | Stronger than MD5, universally available via `sha256sum`/`shasum`/`openssl` |
| 12-char hash prefix             | 48 bits of entropy, collision-free for any practical number of projects |
| Inline timestamp (not sidecar)  | Fewer files, atomic (both written in the same `mv`), simpler cleanup   |
| Pane ID as filename             | One file per pane per project, simple lookup, no index file needed     |
| POSIX shell scripts             | Works on Linux, macOS, WSL without additional dependencies             |
| 24h default staleness           | Matches typical agent session lengths; prevents ghost identities       |

## Interaction with Window Identity (DB-Based)

The MCP Agent Mail server also has a database-backed window identity system (`MCP_AGENT_MAIL_WINDOW_ID`, stored in the `window_identities` table). That system uses a UUID and is managed by the server process. Shared HTTP servers can receive the UUID per request through either `X-MCP-Agent-Mail-Window-ID: <uuid>` or `Authorization: Bearer mcp-window:<uuid>`; this avoids assigning one server-process identity to every connected pane.

The file-based identity contract documented here serves a different purpose: it allows **shell scripts, hooks, and external tools** to discover which agent is running in which pane without needing to query the MCP server. The two systems are complementary:

- File-based: for shell-level identity discovery (hooks, integration scripts, cron jobs).
- DB-based: for server-level identity persistence (MCP tool calls, session continuity).

A local pane or fleet binding remains routing metadata, not Agent Mail sender authority. The request carrier becomes usable only when its UUID is already associated with the requested agent and project. Agent Mail may create or refresh that association after a registration-token-backed call or another call from a session already authenticated as the agent. It refuses to overwrite a UUID associated with a different agent.

Fleet tooling should therefore use this sequence:

1. Create a distinct, persistent UUID for the pane or independently routed client without printing it.
2. Pass it on every HTTP MCP request using one of the request carrier forms above.
3. Authenticate once as the intended Agent Mail identity using its registration token or an already authenticated MCP session.
4. Prove the persisted association with a no-token `whois` call for the same project and agent.
5. Keep shell-level pane routing metadata separate and fail closed if either the live route or Agent Mail proof is missing.

An invalid request carrier deliberately blocks fallback to the process-wide environment value. Treat the UUID like a bearer credential: store it with restrictive permissions, exclude it from logs and prompts, and use TLS outside localhost.

## Fleet launch failure and exact-absence retirement

Fleet launch state is independently roster-visible even before an Agent record
exists. `publish_fleet_launch_state` may create the normalized workspace project
and a nullable-principal `launch_failed` row after an exhausted pre-principal
failure. A higher terminal supervisor sequence fences any older Phase-A command;
an exact late Phase-A replay cannot revive the failed attempt.

Controller crash recovery uses Agent Mail idempotency receipts or exact
state-based terminal replay plus the local supervisor command journal.
Replaying a committed Phase-B activation returns the same runtime binding and
generation. Context/readback convergence remains non-live until its
sequence-fenced observation commits.

That final `ready` observation also reconciles the durable Agent profile's
program, model, task description, and activity timestamp from the exact
generation-fenced runtime binding. Starting, degraded, failed, stale, and
superseded runtimes cannot overwrite the profile. Managed launch therefore
does not require a later token-backed `register_agent` call merely to refresh
profile metadata. An exact healthy heartbeat also reapplies the same projection
so pre-cutover managed runtimes converge without owner ceremony; unhealthy
heartbeats do not rewrite it.

Ending a stale runtime through observation is a separate controller mutation:
`end_fleet_runtime_absent`. It accepts only the exact active runtime binding,
generation, incarnation, pane instance, PID, and process-start timestamp. It
also requires two distinct evidence events—a fresh pane-instance absence and a
fresh provider-process absence—both no older than 45 seconds, plus a prior
healthy observation that has been stale for at least five minutes. The
idempotent transaction:

1. revalidates every runtime and sequence fence;
2. ends the exact runtime generation;
3. persists the final absence observation and both evidence IDs in audit;
4. moves desired state to `held`, retaining the durable principal; and
5. makes the roster project that principal as `durable_only`.

One-sided absence, stale evidence, observer outage, an unreachable host, a
reused pane/PID, or a changed generation fails closed and cannot end a runtime.
