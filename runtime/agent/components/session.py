"""Session-domain component manifest and event subscribers.

This module owns every Role component whose durable source of truth is the
session workspace.  The composition root consumes the manifest; it does not
need to know how individual recorders are constructed.
"""

from __future__ import annotations

from typing import Callable, Optional
from uuid import uuid4

from mote.contracts.events.file.facts import FILE_TRANSACTION_COMMITTED
from mote.contracts.events.governance import SideEffectPolicy
from mote.contracts.ports.events.subscription import (
    CheckpointPolicy,
    EventFilter,
    Ordering,
    OverflowPolicy,
    Reliability,
    RetryPolicy,
    SubscriptionIdentity,
    SubscriptionSpec,
)
from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.agent.component_keys import (
    ARTIFACT_BLOB_STORE,
    ARTIFACT_PUBLISHER,
    ARTIFACT_REPOSITORY_BUNDLE,
    ARTIFACT_RESOLVER,
    ARTIFACT_STORE,
    CHECKPOINT_PAYLOAD_STORE,
    CHECKPOINT_SUBSCRIBER,
    EVENT_FABRIC,
    FILE_OPERATIONS,
    LSP_SERVICE,
    ROUTER,
    RUNTIME_CHECKPOINT_RECORDER,
    RUNTIME_HANDOFF_JOURNAL,
    RUNTIME_OPERATION_JOURNAL,
    RUNTIME_PROJECTION_JOURNAL,
    RUNTIME_PROJECTION_RECONCILER,
    RUNTIME_PROJECTION_REGISTRY,
    SESSION_FACT_COMMITTER,
    SESSION_LOG,
    SESSION_PROJECTION,
    SUBSCRIPTION_STATE_STORE,
    TELEMETRY,
    TITLE_SUBSCRIBER,
    WORKSPACE_STORE,
)
from mote.runtime.artifacts import (
    ArtifactRepositoryBlobStore,
    ArtifactRepositoryBundle,
    ArtifactRepositoryLayout,
    DurableArtifactStore,
    ReliableArtifactPublisher,
    StoreArtifactResolver,
)
from mote.runtime.events.backends import SQLiteSubscriptionStateStore
from mote.runtime.events.dispatcher import SubscriptionBinding, SubscriptionManifest
from mote.runtime.events.fabric import EventFabric
from mote.runtime.fileops import FileOperations
from mote.runtime.interactive import ArtifactCheckpointPayloadStore
from mote.runtime.models.model_calls import generate
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
from mote.runtime.session.artifact_roots import SessionFileOpsArtifactRoots
from mote.runtime.session.checkpoint import checkpoint_supported
from mote.runtime.session.codec import iter_file_operations_events, stable_event_type
from mote.runtime.session.runtime_handoff import SessionRuntimeHandoffJournal
from mote.runtime.session.runtime_operation import SessionRuntimeOperationJournal
from mote.runtime.session.subscribers import CheckpointSubscriber, TitleSubscriber

_SESSION_PROJECTION_MAILBOX_CAPACITY = 1024
_LSP_SUBSCRIPTION = SubscriptionIdentity("mote.lsp.confirmed-file-versions.v1")


def session_component_specs() -> list[ComponentSpec]:
    """Return the complete session-owned portion of the Role component graph."""
    return [
        ComponentSpec(SESSION_LOG, _build_session_log),
        ComponentSpec(SESSION_PROJECTION, _build_session_projection),
        ComponentSpec(SUBSCRIPTION_STATE_STORE, _build_subscription_state_store),
        ComponentSpec(SESSION_FACT_COMMITTER, _build_session_fact_committer),
        ComponentSpec(FILE_OPERATIONS, _build_file_operations),
        ComponentSpec(
            ARTIFACT_REPOSITORY_BUNDLE,
            _build_artifact_repository_bundle,
        ),
        ComponentSpec(ARTIFACT_BLOB_STORE, _build_artifact_blob_store),
        ComponentSpec(ARTIFACT_STORE, _build_artifact_store),
        ComponentSpec(ARTIFACT_RESOLVER, _build_artifact_resolver),
        ComponentSpec(ARTIFACT_PUBLISHER, _build_artifact_publisher),
        ComponentSpec(CHECKPOINT_PAYLOAD_STORE, _build_checkpoint_payload_store),
        ComponentSpec(
            RUNTIME_PROJECTION_JOURNAL,
            _build_runtime_projection_journal,
        ),
        ComponentSpec(
            RUNTIME_HANDOFF_JOURNAL,
            _build_runtime_handoff_journal,
        ),
        ComponentSpec(
            RUNTIME_OPERATION_JOURNAL,
            _build_runtime_operation_journal,
        ),
        ComponentSpec(
            RUNTIME_PROJECTION_REGISTRY,
            _build_runtime_projection_registry,
        ),
        ComponentSpec(
            RUNTIME_PROJECTION_RECONCILER,
            _build_runtime_projection_reconciler,
        ),
        ComponentSpec(CHECKPOINT_SUBSCRIBER, _build_checkpoint_recorder),
        ComponentSpec(TITLE_SUBSCRIBER, _build_title_subscriber),
        ComponentSpec(RUNTIME_CHECKPOINT_RECORDER, _build_runtime_checkpoint_recorder),
    ]


def event_fabric_component_spec() -> ComponentSpec:
    return ComponentSpec(EVENT_FABRIC, _build_event_fabric)


def session_event_subscribers(
    get_checkpoint: Callable[[], CheckpointSubscriber | None],
    get_title: Callable[[], TitleSubscriber | None],
) -> list[CheckpointSubscriber | TitleSubscriber | None]:
    """Build the session-owned slice of the telemetry handler roster."""
    return [
        get_checkpoint(),
        get_title(),
    ]


def _build_session_log(ctx) -> SessionLog:
    # Construction stays I/O-free. Role._emit_session_start commits the first
    # metadata fact at the explicit startup boundary.
    workspace = ctx.dep(WORKSPACE_STORE)
    return SessionLog(
        ctx.role.state.session_id,
        base_dir=str(workspace.sessions_root),
        writer=ctx.role.context.disk_writer,
    )


def _build_session_projection(ctx) -> SessionLiveProjection:
    return SessionLiveProjection(ctx.dep(SESSION_LOG).stream_id)


def _build_subscription_state_store(ctx) -> SQLiteSubscriptionStateStore:
    session_log = ctx.dep(SESSION_LOG)
    return SQLiteSubscriptionStateStore(session_log.path.parent / "subscription-state.sqlite3")


def _build_event_fabric(ctx) -> EventFabric:
    session_log = ctx.dep(SESSION_LOG)
    projection = ctx.dep(SESSION_PROJECTION)
    projection_spec = SubscriptionSpec(
        identity=SESSION_PROJECTION_SUBSCRIPTION,
        event_filter=EventFilter(stream_prefixes=(str(session_log.stream_id),)),
        reliability=Reliability.DURABLE,
        ordering=Ordering.PER_STREAM,
        capacity=_SESSION_PROJECTION_MAILBOX_CAPACITY,
        overflow=OverflowPolicy.BACKPRESSURE,
        retry=RetryPolicy(),
        checkpoint=CheckpointPolicy(persist_every=1),
        side_effect_policy=SideEffectPolicy.TRANSACTIONAL_PROJECTION,
    )
    bindings = [SubscriptionBinding(projection_spec, projection)]
    lsp_service = ctx.dep(LSP_SERVICE)
    if lsp_service is not None:
        bindings.append(
            SubscriptionBinding(
                SubscriptionSpec(
                    identity=_LSP_SUBSCRIPTION,
                    event_filter=EventFilter(
                        event_types=frozenset({stable_event_type(FILE_TRANSACTION_COMMITTED)}),
                        stream_prefixes=(str(session_log.stream_id),),
                    ),
                    reliability=Reliability.RELIABLE,
                    ordering=Ordering.PER_STREAM,
                    capacity=256,
                    overflow=OverflowPolicy.BACKPRESSURE,
                    retry=RetryPolicy(max_attempts=5),
                    checkpoint=CheckpointPolicy(persist_every=1),
                    side_effect_policy=SideEffectPolicy.IDEMPOTENT_EXTERNAL_EFFECT,
                    effect_identity="committed-event-id + confirmed-file-version",
                ),
                lsp_service,
            )
        )
    return EventFabric(
        journal=session_log.event_journal,
        streams=(session_log.stream_id,),
        subscriptions=SubscriptionManifest(tuple(bindings)),
        state_store=ctx.dep(SUBSCRIPTION_STATE_STORE),
        telemetry=ctx.dep(TELEMETRY),
        on_commit=session_log.accept_commit,
    )


def _build_session_fact_committer(ctx) -> SessionFactCommitter:
    return SessionFactCommitter(
        ctx.dep(SESSION_LOG),
        ctx.dep(EVENT_FABRIC),
    )


def _build_file_operations(ctx) -> FileOperations:
    session_log = ctx.dep(SESSION_LOG)
    committer = ctx.dep(SESSION_FACT_COMMITTER)
    session_log.exists()
    role = ctx.role
    bundle = ctx.dep(ARTIFACT_REPOSITORY_BUNDLE)
    return FileOperations(
        session_id=role.state.session_id,
        journal_path=session_log.path,
        get_project_root=lambda: role.state.project_root or role.get_cwd(),
        artifact_repository=bundle.repository,
        artifact_lifecycle_root=session_log.path.parent / "artifact-lifecycle",
        flush_pending=session_log.writer.flush_inline,
        lock_root=session_log.runtime_root / "file-locks",
        event_sink=committer.commit_event_from_thread,
        event_source=lambda: iter_file_operations_events(session_log.iter_events()),
    )


def _build_artifact_repository_bundle(ctx) -> ArtifactRepositoryBundle:
    session_log = ctx.dep(SESSION_LOG)
    layout = ArtifactRepositoryLayout(session_log.workspace_root)
    ownership = layout.ownership(
        session_id=ctx.role.state.session_id,
        project_root=ctx.role.state.project_root or ctx.role.get_cwd(),
    )
    repository = layout.open(ownership).repository
    fileops_artifacts = SessionFileOpsArtifactRoots(session_log.path.parent.parent, repository)
    return layout.open(
        ownership,
        root_sources=(fileops_artifacts,),
        metadata_sources=(fileops_artifacts,),
    )


def _build_artifact_blob_store(ctx) -> ArtifactRepositoryBlobStore:
    bundle = ctx.dep(ARTIFACT_REPOSITORY_BUNDLE)
    return ArtifactRepositoryBlobStore(bundle.repository)


def _build_artifact_store(ctx) -> DurableArtifactStore:
    return ctx.dep(ARTIFACT_REPOSITORY_BUNDLE).store


def _build_artifact_publisher(ctx) -> ReliableArtifactPublisher:
    store = ctx.dep(ARTIFACT_STORE)
    return ReliableArtifactPublisher(store, store)


def _build_artifact_resolver(ctx) -> StoreArtifactResolver:
    return StoreArtifactResolver(ctx.dep(ARTIFACT_STORE))


def _build_checkpoint_payload_store(ctx) -> ArtifactCheckpointPayloadStore:
    secrets_root = ctx.role.wiring.dependencies.secrets_root
    if secrets_root is None:
        raise ValueError("Agent composition requires a secrets root")
    return ArtifactCheckpointPayloadStore(
        ctx.dep(ARTIFACT_STORE),
        build_cipher(
            ctx.role.config.secrets,
            default_key_path=secrets_root / "vault.key",
        ),
    )


def _build_runtime_projection_journal(ctx) -> SessionRuntimeProjectionJournal:
    return SessionRuntimeProjectionJournal(ctx.dep(SESSION_LOG))


def _build_runtime_operation_journal(ctx) -> SessionRuntimeOperationJournal:
    return SessionRuntimeOperationJournal(ctx.dep(SESSION_LOG))


def _build_runtime_handoff_journal(ctx) -> SessionRuntimeHandoffJournal:
    return SessionRuntimeHandoffJournal(ctx.dep(SESSION_LOG))


def _build_runtime_projection_registry(ctx) -> RuntimeProjectionRegistry:
    blobs = ctx.dep(ARTIFACT_BLOB_STORE)
    registry = RuntimeProjectionRegistry()
    registry.register(CanvasArtifactProjector(blobs))
    registry.register(NotebookArtifactProjector(blobs))
    return registry


def _build_runtime_projection_reconciler(ctx) -> RuntimeProjectionReconciler:
    return RuntimeProjectionReconciler(
        ctx.dep(RUNTIME_PROJECTION_REGISTRY),
        ctx.dep(RUNTIME_PROJECTION_JOURNAL),
        ctx.dep(ARTIFACT_PUBLISHER),
        ctx.dep(CHECKPOINT_PAYLOAD_STORE),
    )


def _build_checkpoint_recorder(ctx) -> Optional[CheckpointSubscriber]:
    role = ctx.role
    if not role.role_schema.record_checkpoints:
        return None
    if not checkpoint_supported(role.state.working_dir or None):
        return None
    return CheckpointSubscriber(
        ctx.dep(SESSION_LOG),
        lambda: role.state.project_root or role.state.working_dir,
        ctx.dep(FILE_OPERATIONS).capture_worktree_checkpoint,
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
            ctx.dep(ROUTER).model_route_for_task("session_title"),
            prompt,
            model_call_id=uuid4().hex,
            task="session_title",
            system_prompt=_TITLE_SYSTEM_PROMPT,
            stream=False,
        )
        title = output.content
        return (title or "").strip().strip('"').strip()[:_TITLE_MAX_LEN] or None

    return TitleSubscriber(ctx.dep(SESSION_LOG), _generate, enabled=True)


def _build_runtime_checkpoint_recorder(ctx) -> RuntimeCheckpointRecorder:
    return RuntimeCheckpointRecorder(ctx.dep(SESSION_LOG))
