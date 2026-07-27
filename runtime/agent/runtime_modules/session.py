"""Session-domain component manifest and event subscribers.

This module owns every Role component whose durable source of truth is the
session workspace.  The composition root consumes the manifest; it does not
need to know how individual recorders are constructed.
"""
from __future__ import annotations

from typing import Any, Callable, Optional
from uuid import uuid4

from mote.contracts.ports.event_subscription import (
    CheckpointPolicy,
    EventFilter,
    Ordering,
    OverflowPolicy,
    Reliability,
    RetryPolicy,
    SubscriptionSpec,
)
from mote.kernel.models.model_calls import generate
from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.artifacts import (
    ArtifactRepositoryBlobStore,
    ArtifactRepositoryBundle,
    ArtifactRepositoryLayout,
    DurableArtifactStore,
    LegacyArtifactMigrator,
    ReliableArtifactPublisher,
    StoreArtifactResolver,
)
from mote.runtime.events import EventFabric, SubscriptionBinding, SubscriptionManifest
from mote.runtime.events.backends import SQLiteSubscriptionStateStore
from mote.runtime.fileops import FileOperations
from mote.runtime.interactive import ArtifactCheckpointPayloadStore
from mote.runtime.logging import logger
from mote.runtime.projections import (
    SESSION_PROJECTION_SUBSCRIPTION,
    CanvasArtifactProjector,
    NotebookArtifactProjector,
    RuntimeProjectionReconciler,
    RuntimeProjectionRegistry,
    SessionLiveProjection,
)
from mote.runtime.secrets.cipher import build_cipher
from mote.runtime.session import (
    RuntimeCheckpointRecorder,
    SessionFactCommitter,
    SessionLog,
    SessionRuntimeProjectionJournal,
)
from mote.runtime.session.checkpoint import checkpoint_supported
from mote.runtime.session.codec import iter_file_operations_events
from mote.runtime.session.runtime_handoff import SessionRuntimeHandoffJournal
from mote.runtime.session.runtime_operation import SessionRuntimeOperationJournal
from mote.runtime.session.subscribers import CheckpointSubscriber, TitleSubscriber

_SESSION_PROJECTION_MAILBOX_CAPACITY = 1024


def session_component_specs() -> list[ComponentSpec]:
    """Return the complete session-owned portion of the Role component graph."""
    return [
        ComponentSpec("session_log", _build_session_log),
        ComponentSpec("session_projection", _build_session_projection),
        ComponentSpec("subscription_state_store", _build_subscription_state_store),
        ComponentSpec("event_fabric", _build_event_fabric),
        ComponentSpec("session_fact_committer", _build_session_fact_committer),
        ComponentSpec("file_operations", _build_file_operations),
        ComponentSpec(
            "artifact_repository_bundle",
            _build_artifact_repository_bundle,
        ),
        ComponentSpec("artifact_blob_store", _build_artifact_blob_store),
        ComponentSpec("artifact_store", _build_artifact_store),
        ComponentSpec("artifact_resolver", _build_artifact_resolver),
        ComponentSpec("artifact_publisher", _build_artifact_publisher),
        ComponentSpec("checkpoint_payload_store", _build_checkpoint_payload_store),
        ComponentSpec(
            "runtime_projection_journal",
            _build_runtime_projection_journal,
        ),
        ComponentSpec(
            "runtime_handoff_journal",
            _build_runtime_handoff_journal,
        ),
        ComponentSpec(
            "runtime_operation_journal",
            _build_runtime_operation_journal,
        ),
        ComponentSpec(
            "runtime_projection_registry",
            _build_runtime_projection_registry,
        ),
        ComponentSpec(
            "runtime_projection_reconciler",
            _build_runtime_projection_reconciler,
        ),
        ComponentSpec("checkpoint_subscriber", _build_checkpoint_recorder),
        ComponentSpec("title_subscriber", _build_title_subscriber),
        ComponentSpec("runtime_checkpoint_recorder", _build_runtime_checkpoint_recorder),
    ]


def session_event_subscribers(get: Callable[[str], Any]) -> list:
    """Build the session-owned slice of the telemetry handler roster."""
    return [
        get("checkpoint_subscriber"),
        get("title_subscriber"),
    ]


def _build_session_log(ctx) -> SessionLog:
    # Construction stays I/O-free. Role._emit_session_start commits the first
    # metadata fact at the explicit startup boundary.
    return SessionLog(ctx.role.state.session_id, writer=ctx.role.context.disk_writer)


def _build_session_projection(ctx) -> SessionLiveProjection:
    return SessionLiveProjection(ctx.dep("session_log").stream_id)


def _build_subscription_state_store(ctx) -> SQLiteSubscriptionStateStore:
    session_log = ctx.dep("session_log")
    return SQLiteSubscriptionStateStore(session_log.path.parent / "subscription-state.sqlite3")


def _build_event_fabric(ctx) -> EventFabric:
    session_log = ctx.dep("session_log")
    projection = ctx.dep("session_projection")
    projection_spec = SubscriptionSpec(
        identity=SESSION_PROJECTION_SUBSCRIPTION,
        event_filter=EventFilter(stream_prefixes=(str(session_log.stream_id),)),
        reliability=Reliability.DURABLE,
        ordering=Ordering.PER_STREAM,
        capacity=_SESSION_PROJECTION_MAILBOX_CAPACITY,
        overflow=OverflowPolicy.BACKPRESSURE,
        retry=RetryPolicy(),
        checkpoint=CheckpointPolicy(persist_every=1),
    )
    return EventFabric(
        journal=session_log.event_journal,
        streams=(session_log.stream_id,),
        subscriptions=SubscriptionManifest((SubscriptionBinding(projection_spec, projection),)),
        state_store=ctx.dep("subscription_state_store"),
        telemetry=ctx.dep("telemetry"),
        on_commit=session_log.accept_commit,
    )


def _build_session_fact_committer(ctx) -> SessionFactCommitter:
    return SessionFactCommitter(
        ctx.dep("session_log"),
        ctx.dep("event_fabric"),
    )


def _build_file_operations(ctx) -> FileOperations:
    session_log = ctx.dep("session_log")
    committer = ctx.dep("session_fact_committer")
    session_log.exists()
    role = ctx.role
    return FileOperations(
        session_id=role.state.session_id,
        journal_path=session_log.path,
        get_project_root=lambda: role.state.project_root or role.get_cwd(),
        flush_pending=session_log.writer.flush_inline,
        lock_root=session_log.runtime_root / "file-locks",
        event_sink=committer.commit_event_from_thread,
        event_source=lambda: iter_file_operations_events(session_log.iter_events()),
    )


def _build_artifact_repository_bundle(ctx) -> ArtifactRepositoryBundle:
    session_log = ctx.dep("session_log")
    workspace_root = session_log.path.parent.parent.parent
    migration = LegacyArtifactMigrator(workspace_root).migrate()
    for failure in migration.failures:
        logger.warning(f"Artifact migration preserved source data: {failure}")
    layout = ArtifactRepositoryLayout(workspace_root)
    ownership = layout.ownership(
        session_id=ctx.role.state.session_id,
        project_root=ctx.role.state.project_root or ctx.role.get_cwd(),
    )
    return layout.open(ownership)


def _build_artifact_blob_store(ctx) -> ArtifactRepositoryBlobStore:
    bundle = ctx.dep("artifact_repository_bundle")
    return ArtifactRepositoryBlobStore(bundle.repository)


def _build_artifact_store(ctx) -> DurableArtifactStore:
    return ctx.dep("artifact_repository_bundle").store


def _build_artifact_publisher(ctx) -> ReliableArtifactPublisher:
    store = ctx.dep("artifact_store")
    return ReliableArtifactPublisher(store, store)


def _build_artifact_resolver(ctx) -> StoreArtifactResolver:
    return StoreArtifactResolver(ctx.dep("artifact_store"))


def _build_checkpoint_payload_store(ctx) -> ArtifactCheckpointPayloadStore:
    return ArtifactCheckpointPayloadStore(ctx.dep("artifact_store"), build_cipher(ctx.role.config.secrets))


def _build_runtime_projection_journal(ctx) -> SessionRuntimeProjectionJournal:
    return SessionRuntimeProjectionJournal(ctx.dep("session_log"))


def _build_runtime_operation_journal(ctx) -> SessionRuntimeOperationJournal:
    return SessionRuntimeOperationJournal(ctx.dep("session_log"))


def _build_runtime_handoff_journal(ctx) -> SessionRuntimeHandoffJournal:
    return SessionRuntimeHandoffJournal(ctx.dep("session_log"))


def _build_runtime_projection_registry(ctx) -> RuntimeProjectionRegistry:
    blobs = ctx.dep("artifact_blob_store")
    registry = RuntimeProjectionRegistry()
    registry.register(CanvasArtifactProjector(blobs))
    registry.register(NotebookArtifactProjector(blobs))
    return registry


def _build_runtime_projection_reconciler(ctx) -> RuntimeProjectionReconciler:
    return RuntimeProjectionReconciler(
        ctx.dep("runtime_projection_registry"),
        ctx.dep("runtime_projection_journal"),
        ctx.dep("artifact_publisher"),
        ctx.dep("checkpoint_payload_store"),
    )


def _build_checkpoint_recorder(ctx) -> Optional[CheckpointSubscriber]:
    role = ctx.role
    if not role.role_schema.record_checkpoints:
        return None
    if not checkpoint_supported(role.state.working_dir or None):
        return None
    return CheckpointSubscriber(
        ctx.dep("session_log"),
        lambda: role.state.project_root or role.state.working_dir,
        ctx.dep("file_operations").capture_worktree_checkpoint,
        enabled=True,
    )


_TITLE_SYSTEM_PROMPT = (
    "You name a chat session from its opening message. Reply with ONLY a short "
    "title (at most 6 words, no quotes, no trailing punctuation) capturing the "
    "user's intent. No preamble, no explanation — just the title."
)
_TITLE_MAX_LEN = 80


def _build_title_subscriber(ctx) -> Optional[TitleSubscriber]:
    role = ctx.role
    if not role.role_schema.generate_title:
        return None

    async def _generate(prompt: str) -> Optional[str]:
        output, _resolved = await generate(
            ctx.dep("router").model_route_for_task("session_title"),
            prompt,
            model_call_id=uuid4().hex,
            task="session_title",
            system_prompt=_TITLE_SYSTEM_PROMPT,
            stream=False,
        )
        title = output.content
        return (title or "").strip().strip('"').strip()[:_TITLE_MAX_LEN] or None

    return TitleSubscriber(ctx.dep("session_log"), _generate, enabled=True)


def _build_runtime_checkpoint_recorder(ctx) -> RuntimeCheckpointRecorder:
    return RuntimeCheckpointRecorder(ctx.dep("session_log"))
