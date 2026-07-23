"""SQLModel data models representing agents, messages, projects, and file reservations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Column, Index, UniqueConstraint
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel


def _utcnow_naive() -> datetime:
    """Return current UTC time as a naive datetime for SQLite compatibility.

    SQLite stores datetimes without timezone info. Using naive UTC datetimes
    throughout ensures consistent comparisons and avoids 'can't compare
    offset-naive and offset-aware datetimes' errors in SQLAlchemy ORM evaluator.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True, max_length=255)
    human_key: str = Field(max_length=255, index=True)
    namespace_kind: Optional[str] = Field(default=None, max_length=32, index=True)
    created_at: datetime = Field(default_factory=_utcnow_naive)
    archived_at: Optional[datetime] = Field(default=None)

class Product(SQLModel, table=True):
    """Logical grouping across multiple repositories for product-wide inbox/search and threads."""

    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("product_uid", name="uq_product_uid"), UniqueConstraint("name", name="uq_product_name"))

    id: Optional[int] = Field(default=None, primary_key=True)
    product_uid: str = Field(index=True, max_length=64)
    name: str = Field(index=True, max_length=255)
    created_at: datetime = Field(default_factory=_utcnow_naive)

class ProductProjectLink(SQLModel, table=True):
    """Associates a Project with a Product (many-to-many via link table)."""

    __tablename__ = "product_project_links"
    __table_args__ = (
        UniqueConstraint("product_id", "project_id", name="uq_product_project"),
        Index("idx_product_project", "product_id", "project_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="products.id", index=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    created_at: datetime = Field(default_factory=_utcnow_naive)


class Agent(SQLModel, table=True):
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_agent_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    name: str = Field(index=True, max_length=128)
    program: str = Field(max_length=128)
    model: str = Field(max_length=128)
    task_description: str = Field(default="", max_length=2048)
    inception_ts: datetime = Field(default_factory=_utcnow_naive)
    last_active_ts: datetime = Field(default_factory=_utcnow_naive)
    attachments_policy: str = Field(default="auto", max_length=16)
    contact_policy: str = Field(default="auto", max_length=16)  # open | auto | contacts_only | block_all
    registration_token: Optional[str] = Field(default=None, max_length=64, index=True)
    retired_at: Optional[datetime] = Field(default=None)


class MessageRecipient(SQLModel, table=True):
    __tablename__ = "message_recipients"
    __table_args__ = (
        Index("idx_message_recipients_agent_message", "agent_id", "message_id"),
    )

    message_id: int = Field(foreign_key="messages.id", primary_key=True)
    agent_id: int = Field(foreign_key="agents.id", primary_key=True)
    kind: str = Field(max_length=8, default="to")
    provenance: Optional[str] = Field(default=None, max_length=32, index=True)
    obligation_id: Optional[str] = Field(default=None, max_length=64, unique=True, index=True)
    read_ts: Optional[datetime] = Field(default=None)
    ack_ts: Optional[datetime] = Field(default=None)


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_messages_project_created", "project_id", "created_ts"),
        Index("idx_messages_project_sender_created", "project_id", "sender_id", "created_ts"),
        Index("idx_messages_project_topic", "project_id", "topic"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    sender_id: int = Field(foreign_key="agents.id", index=True)
    thread_id: Optional[str] = Field(default=None, index=True, max_length=128)
    # Direct parent→child reply edge (the specific message this one replies to),
    # distinct from `thread_id` which groups a whole conversation. Nullable: a
    # top-level message replies to nothing. (#188)
    reply_to: Optional[int] = Field(default=None, foreign_key="messages.id", index=True)
    topic: Optional[str] = Field(default=None, max_length=64)
    subject: str = Field(max_length=512)
    body_md: str
    importance: str = Field(default="normal", max_length=16)
    ack_required: bool = Field(default=False)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    attachments: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )


class FileReservation(SQLModel, table=True):
    __tablename__ = "file_reservations"
    __table_args__ = (
        Index("idx_file_reservations_project_released_expires", "project_id", "released_ts", "expires_ts"),
        Index("idx_file_reservations_project_agent_released", "project_id", "agent_id", "released_ts"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    # Nullable so a reservation can outlive its owning agent — when the agent
    # row is deleted (manual cleanup, project hygiene, etc.) the reservation
    # becomes "orphaned" and must still be discoverable so it can be
    # auto-released by the staleness sweeper instead of pinning the path
    # forever. (#161)
    agent_id: Optional[int] = Field(default=None, foreign_key="agents.id", index=True)
    path_pattern: str = Field(max_length=512)
    exclusive: bool = Field(default=True)
    reason: str = Field(default="", max_length=512)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    expires_ts: datetime
    released_ts: Optional[datetime] = None


class AgentLink(SQLModel, table=True):
    """Directed contact link request from agent A to agent B.

    When approved, messages may be sent cross-project between A and B.
    """

    __tablename__ = "agent_links"
    __table_args__ = (UniqueConstraint("a_project_id", "a_agent_id", "b_project_id", "b_agent_id", name="uq_agentlink_pair"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    a_project_id: int = Field(foreign_key="projects.id", index=True)
    a_agent_id: int = Field(foreign_key="agents.id", index=True)
    b_project_id: int = Field(foreign_key="projects.id", index=True)
    b_agent_id: int = Field(foreign_key="agents.id", index=True)
    status: str = Field(default="pending", max_length=16)  # pending | approved | blocked
    reason: str = Field(default="", max_length=512)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    updated_ts: datetime = Field(default_factory=_utcnow_naive)
    expires_ts: Optional[datetime] = None


class WindowIdentity(SQLModel, table=True):
    """Persistent window-based agent identity tied to a tmux/terminal window.

    Agents that share the same window_uuid within a project share a persistent
    identity that survives session restarts, eliminating per-session registration
    overhead and enabling tracking of which window/pane is doing what.
    """

    __tablename__ = "window_identities"
    __table_args__ = (
        UniqueConstraint("project_id", "window_uuid", name="uq_window_identity_project_uuid"),
        Index("idx_window_identities_project_active", "project_id", "expires_ts"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    # Immutable authority target.  ``display_name`` remains presentation and
    # legacy-migration metadata; it must never select a principal once this ID
    # has been established.
    agent_id: Optional[int] = Field(default=None, foreign_key="agents.id", index=True)
    window_uuid: str = Field(max_length=64, index=True)
    display_name: str = Field(max_length=128)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    last_active_ts: datetime = Field(default_factory=_utcnow_naive)
    expires_ts: Optional[datetime] = Field(default=None)
    superseded_ts: Optional[datetime] = Field(default=None, index=True)


class LogicalAgentPrincipal(SQLModel, table=True):
    """Durable supervisor logical-agent assignment to one Agent Mail principal."""

    __tablename__ = "logical_agent_principals"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "logical_agent_key",
            name="uq_logical_agent_principal_project_key",
        ),
        Index(
            "idx_logical_agent_principal_project_agent",
            "project_id",
            "agent_id",
            "retired_ts",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    logical_agent_key: str = Field(max_length=64, index=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    assignment_generation: int = Field(default=1, ge=1)
    assigned_ts: datetime = Field(default_factory=_utcnow_naive)
    retired_ts: Optional[datetime] = Field(default=None)


class ActivePrincipalAuthority(SQLModel, table=True):
    """Database-enforceable selector for one principal's current window authority."""

    __tablename__ = "active_principal_authorities"

    project_id: int = Field(foreign_key="projects.id", primary_key=True)
    agent_id: int = Field(foreign_key="agents.id", primary_key=True)
    window_identity_id: int = Field(
        foreign_key="window_identities.id",
        unique=True,
        index=True,
    )
    authority_generation: int = Field(default=1, ge=1)
    activated_ts: datetime = Field(default_factory=_utcnow_naive)


class FleetLaunchState(SQLModel, table=True):
    """Sequence-fenced desired and coordination projection for the fleet roster."""

    __tablename__ = "fleet_launch_states"

    project_id: int = Field(foreign_key="projects.id", primary_key=True)
    logical_agent_key: str = Field(primary_key=True, max_length=64)
    agent_id: Optional[int] = Field(default=None, foreign_key="agents.id", index=True)
    desired_state: str = Field(default="running", max_length=16)
    launch_attempt_id: Optional[str] = Field(default=None, max_length=64, index=True)
    proof_mode: Optional[str] = Field(default=None, max_length=32)
    coordination_state: str = Field(default="pending", max_length=16)
    identity_context_injected: bool = Field(default=False)
    supervisor_sequence: int = Field(default=0, ge=0)
    expected_generation: int = Field(default=0, ge=0)
    staged_window_identity_id: Optional[int] = Field(
        default=None,
        foreign_key="window_identities.id",
    )
    staged_locator_digest: Optional[str] = Field(default=None, max_length=64)
    provider: Optional[str] = Field(default=None, max_length=128)
    requested_model: Optional[str] = Field(default=None, max_length=128)
    task_description: str = Field(default="", max_length=2048)
    observed_ts: datetime = Field(default_factory=_utcnow_naive)


class RuntimeBinding(SQLModel, table=True):
    """Generation-fenced runtime incarnation and local fleet route."""

    __tablename__ = "runtime_bindings"
    __table_args__ = (
        UniqueConstraint(
            "authority_kind",
            "authority_id",
            "generation",
            name="uq_runtime_binding_authority_generation",
        ),
        Index("idx_runtime_bindings_principal_state", "project_id", "agent_id", "state"),
        Index(
            "idx_runtime_bindings_route_state",
            "host_id",
            "tmux_server_id",
            "pane_id",
            "state",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    authority_kind: str = Field(max_length=32)
    authority_id: str = Field(max_length=64, index=True)
    runtime_session_id: str = Field(max_length=128, index=True)
    runtime_incarnation_id: str = Field(max_length=128, index=True)
    pane_instance_id: str = Field(max_length=128, index=True)
    generation: int = Field(default=1, ge=1)
    host_id: str = Field(max_length=255)
    host_boot_id: str = Field(max_length=255)
    tmux_server_id: str = Field(max_length=255)
    pane_id: str = Field(max_length=64)
    program: str = Field(max_length=128)
    model: str = Field(max_length=128)
    task_description: str = Field(default="", max_length=2048)
    process_id: int = Field(ge=1)
    process_started_ts: datetime
    state: str = Field(default="starting", max_length=32)
    last_heartbeat_ts: datetime = Field(default_factory=_utcnow_naive)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    ended_ts: Optional[datetime] = Field(default=None)


class RuntimeObservation(SQLModel, table=True):
    """Latest sequence-fenced supervisor observation for one runtime binding."""

    __tablename__ = "runtime_observations"

    runtime_binding_id: int = Field(
        foreign_key="runtime_bindings.id",
        primary_key=True,
    )
    runtime_generation: int = Field(ge=1)
    observation_sequence: int = Field(default=0, ge=0)
    observer_id: str = Field(max_length=128)
    pane_live: bool = Field(default=False)
    route_readback_verified: bool = Field(default=False)
    prompt_state: str = Field(default="unknown", max_length=32)
    provider_state: str = Field(default="unknown", max_length=32)
    last_provider_activity_ts: Optional[datetime] = Field(default=None)
    observed_ts: datetime = Field(default_factory=_utcnow_naive)


class InboxCursor(SQLModel, table=True):
    """Durable discovery cursor for one managed inbox consumer."""

    __tablename__ = "inbox_cursors"
    __table_args__ = (
        UniqueConstraint("project_id", "agent_id", "consumer_id", name="uq_inbox_cursor_consumer"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    consumer_id: str = Field(max_length=128)
    cursor: int = Field(default=0, ge=0)
    pending_cursor: int = Field(default=0, ge=0)
    pending_ids_json: str = Field(default="[]")
    discovery_generation: int = Field(default=0, ge=0)
    updated_ts: datetime = Field(default_factory=_utcnow_naive)


class NotificationSignalState(SQLModel, table=True):
    """Monotonic v2 wakeup generation for one immutable recipient."""

    __tablename__ = "notification_signal_states"
    __table_args__ = (
        UniqueConstraint("project_id", "agent_id", name="uq_notification_signal_recipient"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    generation: int = Field(default=0, ge=0)
    max_message_id: int = Field(default=0, ge=0)
    updated_ts: datetime = Field(default_factory=_utcnow_naive)


class StopAttempt(SQLModel, table=True):
    """Atomic hard-stop budget keyed by immutable obligation and policy."""

    __tablename__ = "stop_attempts"
    __table_args__ = (
        UniqueConstraint("obligation_id", "policy_version", name="uq_stop_attempt_policy"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    obligation_id: str = Field(max_length=64, index=True)
    policy_version: str = Field(max_length=64)
    attempts_allocated: int = Field(default=0, ge=0)
    last_attempt_ts: Optional[datetime] = Field(default=None)
    created_ts: datetime = Field(default_factory=_utcnow_naive)


class ContinuationReceipt(SQLModel, table=True):
    """Replay-tracked digest and scope for a signed continuation receipt."""

    __tablename__ = "continuation_receipts"

    nonce: str = Field(primary_key=True, max_length=64)
    token_digest: str = Field(min_length=64, max_length=64, unique=True, index=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    authority_kind: str = Field(max_length=32)
    authority_id: str = Field(max_length=64)
    runtime_session_id: str = Field(max_length=128)
    runtime_incarnation_id: str = Field(max_length=128)
    pane_instance_id: str = Field(max_length=128)
    process_id: int = Field(default=0, ge=0)
    host_boot_id: str = Field(default="", max_length=255)
    prior_generation: int = Field(ge=1)
    carrier_digest: str = Field(min_length=64, max_length=64)
    target_locator_digest: Optional[str] = Field(default=None, max_length=64)
    reserved_launch_attempt_id: Optional[str] = Field(default=None, max_length=64)
    reserved_ts: Optional[datetime] = Field(default=None)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    expires_ts: datetime
    consumed_ts: Optional[datetime] = Field(default=None)


class MessageSummary(SQLModel, table=True):
    """Stored on-demand project-wide message summary."""

    __tablename__ = "message_summaries"
    __table_args__ = (
        Index("idx_summaries_project_end", "project_id", "end_ts"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    summary_text: str
    start_ts: datetime
    end_ts: datetime
    source_message_count: int = Field(default=0)
    source_thread_ids: str = Field(default="[]")  # JSON array of thread IDs
    llm_model: Optional[str] = Field(default=None, max_length=128)
    cost_usd: Optional[float] = Field(default=None)
    created_ts: datetime = Field(default_factory=_utcnow_naive)


class AuditHead(SQLModel, table=True):
    """Latest verified audit-chain position for one project."""

    __tablename__ = "audit_heads"

    project_id: int = Field(foreign_key="projects.id", primary_key=True)
    last_sequence: int = Field(default=0, ge=0)
    last_event_hash: str = Field(default="0" * 64, min_length=64, max_length=64)


class AuditEvent(SQLModel, table=True):
    """Immutable, per-project ordered mutation event."""

    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("project_id", "project_sequence", name="uq_audit_event_project_sequence"),
        UniqueConstraint("event_hash", name="uq_audit_event_hash"),
        Index("idx_audit_events_project_created", "project_id", "created_ts"),
        Index("idx_audit_events_actor", "actor_kind", "actor_scope_id"),
        Index("idx_audit_events_entity", "entity_type", "entity_id"),
        Index("idx_audit_events_operation", "operation_kind"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    project_sequence: int = Field(ge=1)
    actor_kind: str = Field(max_length=32)
    actor_scope_id: str = Field(max_length=255)
    actor_agent_id: Optional[int] = Field(default=None, foreign_key="agents.id", index=True)
    operation_kind: str = Field(max_length=128)
    entity_type: str = Field(max_length=64)
    entity_id: str = Field(max_length=255)
    payload_version: str = Field(max_length=64)
    payload_json: str
    previous_event_hash: str = Field(min_length=64, max_length=64)
    event_hash: str = Field(min_length=64, max_length=64)
    created_ts: datetime = Field(default_factory=_utcnow_naive)


class Blob(SQLModel, table=True):
    """Durable content-addressed object metadata."""

    __tablename__ = "blobs"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_blob_storage_key"),
        Index("idx_blobs_verification_created", "verification_state", "created_ts"),
    )

    digest: str = Field(primary_key=True, min_length=64, max_length=64)
    byte_length: int = Field(ge=0)
    media_type: str = Field(default="application/octet-stream", max_length=255)
    storage_key: str = Field(max_length=512)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    verification_state: str = Field(default="verified", max_length=32)


class BlobReference(SQLModel, table=True):
    """Links a content-addressed object to a domain entity."""

    __tablename__ = "blob_references"
    __table_args__ = (
        UniqueConstraint(
            "blob_digest",
            "entity_type",
            "entity_id",
            "role",
            name="uq_blob_reference_entity_role",
        ),
        Index("idx_blob_references_entity", "entity_type", "entity_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    blob_digest: str = Field(foreign_key="blobs.digest", index=True, max_length=64)
    entity_type: str = Field(max_length=64)
    entity_id: str = Field(max_length=255)
    role: str = Field(max_length=64)
    display_name: str = Field(default="", max_length=512)
    created_ts: datetime = Field(default_factory=_utcnow_naive)


class IdempotencyRecord(SQLModel, table=True):
    """Immutable terminal receipt for one authenticated mutation identity."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "scope_kind",
            "scope_id",
            "operation_kind",
            "idempotency_key",
            name="uq_idempotency_scope_operation_key",
        ),
        Index("idx_idempotency_project_created", "project_id", "created_ts"),
        Index("idx_idempotency_expires", "expires_ts"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    scope_kind: str = Field(max_length=32)
    scope_id: str = Field(max_length=255)
    project_id: Optional[int] = Field(default=None, foreign_key="projects.id", index=True)
    operation_kind: str = Field(max_length=128)
    idempotency_key: str = Field(max_length=255)
    fingerprint_version: str = Field(max_length=64)
    request_fingerprint: str = Field(min_length=64, max_length=64)
    response_json: str
    entity_type: str = Field(max_length=64)
    entity_id: str = Field(max_length=255)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    expires_ts: datetime = Field(index=True)


class PaneCredential(SQLModel, table=True):
    """Durable, secret-backed binding from one pane to one agent identity."""

    __tablename__ = "pane_credentials"
    __table_args__ = (
        UniqueConstraint("project_id", "window_uuid", name="uq_pane_project_window"),
        Index("idx_pane_credentials_agent_active", "agent_id", "revoked_ts", "expires_ts"),
    )

    id: str = Field(primary_key=True, max_length=64)
    project_id: int = Field(foreign_key="projects.id", index=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    window_uuid: str = Field(max_length=128, index=True)
    secret_digest: str = Field(min_length=64, max_length=64)
    pepper_key_id: str = Field(max_length=64)
    generation: int = Field(default=1, ge=1)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    last_used_ts: datetime = Field(default_factory=_utcnow_naive)
    expires_ts: Optional[datetime] = Field(default=None)
    revoked_ts: Optional[datetime] = Field(default=None)
    revoke_reason: str = Field(default="", max_length=512)


class BootstrapCredential(SQLModel, table=True):
    """Short-lived, single-use authority for a managed first registration."""

    __tablename__ = "bootstrap_credentials"
    __table_args__ = (
        Index("idx_bootstrap_credentials_project_expiry", "project_id", "expires_ts"),
    )

    id: str = Field(primary_key=True, max_length=64)
    project_id: Optional[int] = Field(default=None, foreign_key="projects.id", index=True)
    prospective_project_digest: Optional[str] = Field(default=None, max_length=64)
    secret_digest: str = Field(min_length=64, max_length=64)
    pepper_key_id: str = Field(max_length=64)
    window_uuid: str = Field(max_length=128)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    expires_ts: datetime
    consumed_ts: Optional[datetime] = Field(default=None)
    consumed_agent_id: Optional[int] = Field(default=None, foreign_key="agents.id")
    consumed_idempotency_key: Optional[str] = Field(default=None, max_length=255)
    revoked_ts: Optional[datetime] = Field(default=None)


class ProjectStorageCutover(SQLModel, table=True):
    """Transactionally authoritative storage route for one project."""

    __tablename__ = "project_storage_cutovers"
    __table_args__ = (Index("idx_project_storage_cutovers_state", "state", "updated_ts"),)

    project_id: int = Field(foreign_key="projects.id", primary_key=True)
    state: str = Field(default="legacy", max_length=32)
    generation: int = Field(default=1, ge=1)
    baseline_event_id: Optional[int] = Field(default=None, foreign_key="audit_events.id")
    manifest_digest: Optional[str] = Field(default=None, max_length=64)
    started_ts: datetime = Field(default_factory=_utcnow_naive)
    updated_ts: datetime = Field(default_factory=_utcnow_naive)
    completed_ts: Optional[datetime] = Field(default=None)
    last_error_code: Optional[str] = Field(default=None, max_length=128)


class ProjectSiblingSuggestion(SQLModel, table=True):
    """LLM-ranked sibling project suggestion (undirected pair)."""

    __tablename__ = "project_sibling_suggestions"
    __table_args__ = (UniqueConstraint("project_a_id", "project_b_id", name="uq_project_sibling_pair"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    project_a_id: int = Field(foreign_key="projects.id", index=True)
    project_b_id: int = Field(foreign_key="projects.id", index=True)
    score: float = Field(default=0.0)
    status: str = Field(default="suggested", max_length=16)  # suggested | confirmed | dismissed
    rationale: str = Field(default="", max_length=4096)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    evaluated_ts: datetime = Field(default_factory=_utcnow_naive)
    confirmed_ts: Optional[datetime] = Field(default=None)
    dismissed_ts: Optional[datetime] = Field(default=None)
