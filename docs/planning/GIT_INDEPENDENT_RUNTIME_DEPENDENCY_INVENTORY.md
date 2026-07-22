# Git-independent runtime dependency inventory

**Snapshot:** 2026-07-21  
**Repository baseline:** `main` at `132cb16` plus the inventory commit that adds this file  
**Purpose:** classify every known Git/archive dependency before changing Agent Mail's storage authority

“Git-independent” applies to Agent Mail's required server runtime and synchronous coordination path. It does not remove Git from user source repositories. Source-repository guards, identity discovery, and static/Git exports remain available only through isolated optional components that cannot affect core readiness or mutation success.

## Classification contract

| Code | Required disposition |
|---|---|
| `DB-AUDIT` | Replace with relational state plus an audit event in the same SQLite transaction. |
| `BLOB` | Replace with the non-Git content-addressed blob store and DB references. |
| `DERIVED` | Render from DB/audit/blob state; never read an internal archive during a core request. |
| `MIGRATION` | Retain only in the temporary migration profile for projects still in `legacy`. |
| `REPO-OPTIONAL` | User source-repository integration; isolate behind a lazy optional adapter/extra. |
| `EXPORT-OPTIONAL` | Static/Git export; isolate in a separate invocation/process. |
| `REMOVE` | Delete after all projects are `git_independent` and parity has passed. |

No row classified `MIGRATION`, `REPO-OPTIONAL`, or `EXPORT-OPTIONAL` may be imported by the final core package. A `git_independent` project must fail a test if it resolves or calls the legacy adapter.

## Core storage and archive primitives

| Surface | Current responsibility | Disposition | Replacement and acceptance test |
|---|---|---|---|
| `storage.py:78-352` `_CommitRequest`, `_CommitQueue` | Serializes Git commits and retries index-lock failures. | `MIGRATION` then `REMOVE` | Migration adapter only. Core mutation success cannot enqueue or await it; exporter owns its own cursor. |
| `storage.py:383-419` `ProjectArchive` | Bundles repo root, project directory, `Repo`, settings, and attachment paths. | Split | Replace core use with a Git-neutral project storage context; legacy form remains only in migration/export adapters. |
| `storage.py:398-1264` repo cache, FD cleanup, `AsyncFileLock` | Manages Git repository handles and archive/commit locks. | `MIGRATION` then `REMOVE` | Core has no repo cache, archive lock, commit lock, `.git/index.lock`, or Git FD telemetry. SQLite/blob leases replace only the concurrency controls actually needed. |
| `storage.py:1266` `archive_write_lock` | Serializes project archive mutations. | `MIGRATION` then `REMOVE` | SQLite transaction plus blob installation/backup leases; no archive lock on core reads or writes. |
| `storage.py:1358` `collect_lock_status` | Reports archive/commit lock state. | `MIGRATION` | Migration-profile health only. Core health reports DB, audit, blob, and lease state. |
| `storage.py:1447-1525` `ensure_archive_root`, `ensure_archive`, `_ensure_repo` | Creates/opens the shared internal Git archive and initializes `.gitattributes`. | `MIGRATION` then `REMOVE` | Core startup and requests never call these functions or create/open `.git`. |
| `storage.py:1527` `write_agent_profile` | Writes `agents/<name>/profile.json` and commits it. | `DB-AUDIT` + `DERIVED` | Agent row and audit event are atomic; profile JSON is export-only. |
| `storage.py:1543-1579` reservation writers | Writes reservation JSON aliases and commits them. | `DB-AUDIT` + `DERIVED` | Reservation mutation and audit event are atomic; guard snapshots are DB/API-derived. |
| `storage.py:1581-1776` `write_message_bundle`, thread digest | Writes canonical message, inbox/outbox copies, and thread digest, then commits. | `DB-AUDIT` + `DERIVED` | Message, recipients, idempotency receipt, and audit event commit together; all Markdown/mailbox/thread views derive afterward. |
| `storage.py:1778-2050` attachment and Markdown image processing | Places originals, WebP files, manifests, and attachment audit JSON under the archive. | `BLOB` | Durable no-replace SHA-256 blob install precedes DB reference; corrupt collisions quarantine; GC honors install/snapshot leases. |
| `storage.py:2049` `_append_attachment_audit` | Appends JSONL in the archive. | `DB-AUDIT` | Structured audit event/provenance row; no append-only filesystem authority. |
| `storage.py:2071-2351` commit locks, `_commit_direct`, `_commit` | Stages paths, commits Git history, handles stale index locks. | `MIGRATION` then `REMOVE` | No final-core equivalent. SQLite commit is the coordination success boundary. |
| `storage.py:2353` `heal_archive_locks` | Repairs archive and commit lock artifacts. | `MIGRATION` | Available only while legacy projects exist; absent from final core health/startup. |
| `storage.py:2419-2653` recent commit/detail APIs | Reads Git log, diffs, authors, and stats. | `DERIVED` | Audit-event timeline/detail queries; optional legacy SHA provenance is not a live identifier. |
| `storage.py:2655` `get_message_commit_sha` | Finds Git commit for a canonical message file. | `DERIVED` | Return audit event ID/hash; nullable legacy SHA comes only from import provenance. |
| `storage.py:2706-2853` archive tree/file reads | Browses internal archive paths and Git blobs. | `DERIVED` / `EXPORT-OPTIONAL` | Core viewer uses explicit DB/audit/blob resources; raw archive browsing moves to frozen-artifact tooling. |
| `storage.py:2855-3221` communication graph, timeline, historical inbox | Reconstructs views from Git commits/files. | `DERIVED` | SQL/audit queries or materialized DB views, with replay from the signed baseline. |
| `storage.py:3223-3659` diagnostic backup/restore | Packages SQLite, archive files, and Git bundles and restores them. | Split | Core backup is a consistent SQLite/blob/checkpoint package; legacy Git bundle remains migration-artifact-only. |
| `storage.py:3661-3806` notification signals | Stores ephemeral signal files below storage paths. | `DB-AUDIT` or explicit runtime state | Move durable notification state to DB; if OS wakeup files remain, they are non-authoritative and outside the blob/audit model. |

## MCP application call sites

| Surface | Current coupling | Disposition |
|---|---|---|
| `app.py:37-38`, `185-224` | Module-level GitPython imports and repo/context helpers. | `REPO-OPTIONAL`; remove imports from core module and inject a lazy repo-integration interface. |
| `app.py:1421-1550` reservation filesystem/Git activity | Uses repository history to infer recent activity. | Replace core staleness with explicit DB timestamps/audit activity. Optional source-repo activity may enrich output asynchronously but cannot gate reservations. |
| `app.py:2100-2460` project identity discovery | Reads source-repo markers, remotes, common dir, branch, and worktree. | `REPO-OPTIONAL`; explicit project UID/DB identity is authoritative. |
| `app.py:2921` `_archive_write_lock` wrapper | Converts archive lock timeout into tool error. | `MIGRATION` then `REMOVE`. |
| `app.py:3386-3394` generated agent-name check | Checks archive directory existence. | `DB-AUDIT`; rely exclusively on DB unique constraint and bounded retry. |
| `app.py:3622-3642` agent registration profile write/rollback | Commits DB, then writes Git, then compensates DB on failure. | `DB-AUDIT`; one SQLite transaction, no compensation across DB/Git. |
| `app.py:4036-4072`, `4396` reservation artifact writes | Writes reservation JSON after DB mutations. | `DB-AUDIT` + `DERIVED`. |
| `app.py:4698-4773` canonical path and commit metadata | Maps messages to archive files and reads Git diffs. | `DERIVED`; audit event metadata replaces commit context. |
| `app.py:5576-5790` `_deliver_message` | Commits message/recipients, writes bundle under archive lock, compensates DB on failure. | `DB-AUDIT` + `BLOB`; primary target for atomic mutation framework. |
| `app.py:6051` project ensure path | Opens/creates an archive during project creation. | `MIGRATION`; new core projects initialize DB/audit/blob state only. |
| `app.py:6540-6545` agent existence/archive profile path | Treats archive directory as identity evidence. | `DB-AUDIT`; remove archive check. |
| `app.py:6773-6829` `whois(...include_recent_commits)` | Reads recent archive commits. | `DERIVED`; return recent audit activity, with optional exporter provenance separately. |
| `app.py:6929-6944` agent profile update | DB commit followed by Git profile write and compensation. | `DB-AUDIT`. |
| `app.py:11513-11560` guard install/uninstall tools | Operates user repository hooks. | `REPO-OPTIONAL`; not part of final core dependency graph, while normal user Git workflows remain supported. |
| `app.py:11691-12366` reservation lifecycle | Opens archive, writes records, uses Git activity in stale explanations. | `DB-AUDIT`; explicit reservation/agent/message timestamps replace Git-derived authority. |
| `app.py:12383-12590` build-slot files | Stores slot state below archive root under archive locks. | `DB-AUDIT`; build slots become relational leases and audit events. |
| `app.py:14329-14862` mailbox-with-commit resources | Adds Git commit details to recent messages/outbox. | `DERIVED`; audit event ID/hash and timeline replace commit metadata. |

## HTTP viewer and write call sites

| Surface | Current coupling | Disposition |
|---|---|---|
| `http.py:48-65` | Imports `ProjectArchive`, archive locks/readers/writers at module load. | Remove from final core import graph; inject migration/view adapters lazily. |
| `http.py:113-155` human-overseer bootstrap/profile | DB mutation followed by agent-profile Git write. | `DB-AUDIT`. |
| `http.py:230-287` web message deletion | Deletes canonical/inbox/outbox files and commits Git after DB delete. | `DB-AUDIT` + blob-reference release; derived views need no deletion. |
| `http.py:315-326` `_open_existing_project_archive` | Opens a Git repo for viewer routes. | `MIGRATION` / `EXPORT-OPTIONAL`; final viewer uses DB/audit/blob. |
| `http.py:393-441` guide statistics | Counts commits and reads latest commit date. | `DERIVED`; use audit counts/head timestamp. |
| `http.py:1296-1302` maintenance agent retirement | Writes archive profile under lock. | `DB-AUDIT`. |
| `http.py:2243-2375`, `3064-3257` web mutations | DB commits plus archive cleanup/commit. | Route through the shared atomic mutation service. |
| `http.py:2941-2994` message provenance badge | Resolves message commit SHA. | Audit event ID/hash; legacy SHA optional. |
| `http.py:3857-3896` Human Overseer send | DB commit then `write_message_bundle`. | Same atomic message mutation service used by MCP transport. |
| `http.py:3956-4105` archive activity/commit/timeline/tree routes | Direct Git log/diff/tree UI. | DB/audit/blob viewer; frozen legacy archive viewer is separate migration tooling. |

## CLI, guard, export, and packaging

| Surface | Current coupling | Disposition |
|---|---|---|
| `cli.py:97` module-level storage imports | Pulls archive/Git code into every CLI command. | Lazy command-local imports; final core commands do not import legacy adapter. |
| `cli.py:3435-3439` agent archive inspection | Reads agent directory. | DB query. |
| `cli.py:3770-4448` guard and project-marker commands | Invokes Git in user source repositories. | `REPO-OPTIONAL`; preserve functionality outside required core. |
| `cli.py:3995-4100` build-slot file commands | Reads/writes archive paths under locks. | DB lease commands. |
| `cli.py:4463-4732` project adopt/move | Moves archive files and creates Git commits. | `MIGRATION`; replace durable project relinking with DB transaction and blob-reference reassignment. |
| `cli.py:5480-5830` doctor/backup/lock repair | Diagnoses Git/archive locks and bundles. | Split into core DB/audit/blob doctor and migration-only legacy doctor. |
| `guard.py:12,140-745` | Uses internal reservation JSON to generate hooks; generated hooks invoke Git against user repos. | Reservation lookup moves to authenticated DB/API snapshot; hook Git usage is `REPO-OPTIONAL`, not internal archive authority. |
| `share.py` | Builds static shares and invokes external render/tooling commands. | `EXPORT-OPTIONAL`; read a consistent core snapshot/API and run separately. |
| `scripts/share_to_github_pages.py` | Initializes, commits, and force-pushes an export repository. | `EXPORT-OPTIONAL`; never imported or supervised by core. |
| `pyproject.toml:49` | Lists GitPython as a required dependency. | Move to migration/repo/export extras; core install must succeed without it. |

## Preserved source-repository Git behavior

The following capabilities operate on user repositories rather than Agent Mail's internal archive. They remain supported, but outside the required core dependency graph:

- project identity discovery from committed markers, remotes, branches, and worktrees;
- pre-commit/pre-push guard installation and execution;
- optional source-repository activity enrichment;
- explicit CLI commands that create/commit a project identity marker;
- static/Git/GitHub Pages export.

Core must expose stable interfaces for these adapters and return an explicit `optional_component_unavailable` response when an optional adapter is absent. Missing GitPython or `git` must never make core startup, health, messaging, registration, reservations, search, backup, or restore fail.

## Verification commands and exit gates

The inventory remains incomplete while an unexplained match exists in any command below:

```bash
rg -n "ensure_archive(?:_root)?|ProjectArchive|archive_write_lock|archive\.root|archive\.repo" src/mcp_agent_mail
rg -n "from git|import git|Repo\(|iter_commits|hexsha|\.git\.|index\.lock" src/mcp_agent_mail
rg -n "write_message_bundle|write_agent_profile|write_file_reservation|_commit_direct|_commit\(" src/mcp_agent_mail
rg -n "subprocess\.(run|Popen)|\[\"git\"|\['git'" src/mcp_agent_mail scripts
```

Phase 0 exits only when:

1. every match is mapped above or explicitly added with owner, replacement, compatibility effect, and test;
2. the core/migration/repo/export import boundaries are named in packaging;
3. test fixtures cover messages, BCC, attachments, profiles, reservations, releases, build slots, timelines, graphs, historical inboxes, web mutations, Human Overseer sends, backups, and frozen archive restore;
4. the no-internal-Git core gate and mixed-state migration routing gate have executable test commands.
