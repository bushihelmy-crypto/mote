"""Structural interfaces (PEP 544 Protocols) for the react-loop collaborators.

These describe the *narrow slice* each consumer needs from a duck-typed
dependency, so the assembly site (Role) is statically checked: if an
implementation drifts from what the loop/channel/think-engine relies on, a
type checker flags it at the injection point instead of failing at runtime.

Why Protocol, not ABC: the collaborators have multiple unrelated implementations
(real ``ContextManager`` + ``FakeLLM`` in tests + provider clients) that must NOT
share a base class. Protocols are structural — anything shaped right conforms,
no inheritance required — which preserves the framework's duck-typing while
still giving static guarantees.

Interface segregation: a single object may satisfy several of these (e.g.
``ContextManager`` is both a ``MessageStore`` and a ``RequestAssembler``), but
each consumer depends only on the face it uses — mirroring the tool-capability
allowlist, extended from the tool<->Role boundary to component<->component edges.

This is a LEAF package: it imports only ``typing`` and (under TYPE_CHECKING) the
``Message`` type, so it can be imported from anywhere without risking a cycle.
"""

from mote.contracts.ports.agent_factory import AgentFactory
from mote.contracts.ports.artifact_store import (
    ArtifactBlobStore,
    ArtifactPublicationOutbox,
    ArtifactResolver,
    ArtifactStore,
    ReliableArtifactPublisher,
)
from mote.contracts.ports.canvas_backend import (
    CanvasBackend,
    CanvasBackendCapabilities,
    CanvasBackendRender,
    CanvasBackendSession,
    CanvasExportPort,
)
from mote.contracts.ports.commit_fence import CommitFence
from mote.contracts.ports.compaction_policy import (
    CompactionPolicy,
    CompactionPolicyExtension,
    CompactionPolicyExtensionFactory,
    CompactionPolicyExtensionSpec,
)
from mote.contracts.ports.completion_policy import CompletionPolicy
from mote.contracts.ports.context_reducer import ContextReducer
from mote.contracts.ports.event_journal import (
    AppendResult,
    EventJournal,
    EventJournalError,
    JournalIntegrityError,
    StreamVersionConflict,
    UncommittedFact,
    VerificationIssue,
    VerificationReport,
)
from mote.contracts.ports.file_changes import FileChangePort
from mote.contracts.ports.hook_runner import HookRunner
from mote.contracts.ports.human_interaction import HumanInteractionPort
from mote.contracts.ports.lease import LeaseCoordinator
from mote.contracts.ports.lease import LeaseEpoch as GenericLeaseEpoch
from mote.contracts.ports.llm_client import LLMClient
from mote.contracts.ports.message_activity import MessageActivity
from mote.contracts.ports.message_sink import MessageSink
from mote.contracts.ports.message_store import MessageStore
from mote.contracts.ports.model_call_journal import ModelCallJournal
from mote.contracts.ports.model_endpoint import ModelEndpointAdapter, ModelEndpointResolver
from mote.contracts.ports.model_gateway import ModelGateway, ModelRoute
from mote.contracts.ports.model_operator import ModelOperatorAuditStore, ModelOperatorControl
from mote.contracts.ports.model_request_transformer import ModelRequestTransformer
from mote.contracts.ports.output import OutputDecoder, OutputEngine, OutputValidator
from mote.contracts.ports.output_migration import OutputMigration
from mote.contracts.ports.prompt_policy import (
    PromptPolicy,
    PromptPolicyExtension,
    PromptPolicyExtensionFactory,
    PromptPolicyExtensionSpec,
)
from mote.contracts.ports.request_assembler import RequestAssembler
from mote.contracts.ports.resource_loader import ResourceProvider
from mote.contracts.ports.routing import RoutingPolicy, RoutingStateStore
from mote.contracts.ports.run_completion_policy import (
    RunCompletionPolicy,
    RunCompletionPolicyExtension,
    RunCompletionPolicyExtensionFactory,
    RunCompletionPolicyExtensionSpec,
)
from mote.contracts.ports.run_lease import LeaseEpoch, RunLeaseCoordinator
from mote.contracts.ports.runtime_checkpoint import RuntimeCheckpointPayloadStore, RuntimeCheckpointSink
from mote.contracts.ports.runtime_driver import (
    HandoffRuntimeDriver,
    JournaledRuntimeDriver,
    LiveSurfaceRuntimeDriver,
    ManagedRuntimeDriver,
    ObservableSurfaceRuntimeDriver,
)
from mote.contracts.ports.runtime_handoff import RuntimeHandoffJournal
from mote.contracts.ports.runtime_operation import RuntimeOperationJournal
from mote.contracts.ports.runtime_projection import RuntimeProjectionJournal, RuntimeProjector
from mote.contracts.ports.service_call_journal import ServiceCallJournal
from mote.contracts.ports.service_endpoint import ServiceEndpointAdapter, ServiceEndpointResolver
from mote.contracts.ports.service_gateway import ServiceGateway
from mote.contracts.ports.session_facts import SessionFactSink
from mote.contracts.ports.spawn_policy import (
    SpawnAdmissionPolicy,
    SpawnPolicyExtension,
    SpawnPolicyExtensionFactory,
    SpawnPolicyExtensionSpec,
)
from mote.contracts.ports.surface_presenter import LiveSurfacePresenter, SurfacePresentationSession
from mote.contracts.ports.tool_policy import (
    PermissionFactsResolver,
    ToolCallPolicy,
    ToolCallPolicyExtension,
    ToolCallPolicyExtensionFactory,
    ToolCallPolicyExtensionSpec,
    ToolResultPolicy,
)
from mote.contracts.ports.turn_context import DEFAULT_TURN_CONTEXT_PRIORITY, EphemeralContextSource, TurnContextPriority
from mote.contracts.ports.window_surface import LiveWindowBackend, LiveWindowBackendSession, SurfaceInputHandler

__all__ = [
    "CompletionPolicy",
    "CompactionPolicy",
    "CompactionPolicyExtension",
    "CompactionPolicyExtensionFactory",
    "CompactionPolicyExtensionSpec",
    "PromptPolicy",
    "PromptPolicyExtension",
    "PromptPolicyExtensionFactory",
    "PromptPolicyExtensionSpec",
    "RunCompletionPolicy",
    "RunCompletionPolicyExtension",
    "RunCompletionPolicyExtensionFactory",
    "RunCompletionPolicyExtensionSpec",
    "SpawnAdmissionPolicy",
    "SpawnPolicyExtension",
    "SpawnPolicyExtensionFactory",
    "SpawnPolicyExtensionSpec",
    "ToolCallPolicy",
    "ToolCallPolicyExtension",
    "ToolCallPolicyExtensionFactory",
    "ToolCallPolicyExtensionSpec",
    "ToolResultPolicy",
    "PermissionFactsResolver",
    "CommitFence",
    "OutputDecoder",
    "OutputEngine",
    "OutputValidator",
    "OutputMigration",
    "MessageStore",
    "ModelGateway",
    "ModelRoute",
    "ModelCallJournal",
    "ModelOperatorAuditStore",
    "ModelOperatorControl",
    "ModelEndpointAdapter",
    "ModelEndpointResolver",
    "ModelRequestTransformer",
    "RequestAssembler",
    "LeaseEpoch",
    "RunLeaseCoordinator",
    "RoutingPolicy",
    "RoutingStateStore",
    "SessionFactSink",
    "ServiceEndpointAdapter",
    "ServiceEndpointResolver",
    "ServiceCallJournal",
    "ServiceGateway",
    "LeaseCoordinator",
    "GenericLeaseEpoch",
    "ManagedRuntimeDriver",
    "HandoffRuntimeDriver",
    "JournaledRuntimeDriver",
    "LiveSurfaceRuntimeDriver",
    "ObservableSurfaceRuntimeDriver",
    "RuntimeCheckpointSink",
    "RuntimeCheckpointPayloadStore",
    "RuntimeHandoffJournal",
    "RuntimeProjectionJournal",
    "RuntimeOperationJournal",
    "RuntimeProjector",
    "HumanInteractionPort",
    "LLMClient",
    "MessageActivity",
    "MessageSink",
    "HookRunner",
    "FileChangePort",
    "ResourceProvider",
    "CanvasBackend",
    "CanvasBackendCapabilities",
    "CanvasBackendRender",
    "CanvasBackendSession",
    "CanvasExportPort",
    "AgentFactory",
    "ArtifactBlobStore",
    "ArtifactPublicationOutbox",
    "ArtifactResolver",
    "ArtifactStore",
    "ReliableArtifactPublisher",
    "ContextReducer",
    "EphemeralContextSource",
    "TurnContextPriority",
    "DEFAULT_TURN_CONTEXT_PRIORITY",
    "AppendResult",
    "EventJournal",
    "EventJournalError",
    "JournalIntegrityError",
    "StreamVersionConflict",
    "UncommittedFact",
    "VerificationIssue",
    "VerificationReport",
    "LiveSurfacePresenter",
    "SurfacePresentationSession",
    "LiveWindowBackend",
    "LiveWindowBackendSession",
    "SurfaceInputHandler",
]
