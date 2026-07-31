"""Typed identities for Role components shared across assembly manifests."""

from __future__ import annotations

from collections.abc import Callable

from mote.contracts.ports.artifact.store import ArtifactResolver, ArtifactStore
from mote.contracts.ports.artifact.store import ReliableArtifactPublisher as ArtifactPublisherPort
from mote.contracts.ports.code_intelligence.code_map import CodeMapIndexer
from mote.contracts.ports.code_intelligence.lsp import DiagnosticsProvider
from mote.contracts.ports.conversation.prompt_policy import PromptPolicy
from mote.contracts.ports.conversation.turn_context import EphemeralContextSource
from mote.contracts.ports.output.run_completion_policy import RunCompletionPolicy
from mote.contracts.ports.skill.registry import SkillService
from mote.contracts.ports.task.operations import BackgroundTaskService
from mote.contracts.ports.tool.policy import ToolCallPolicy, ToolResultPolicy
from mote.kernel.commands import CommandChannel
from mote.kernel.inference.base import BaseInferenceEngine
from mote.kernel.inference.prompt_builder import InferenceSubsystems
from mote.runtime.agent.capabilities import RoleCapabilities
from mote.runtime.agent.component_graph import ComponentKey
from mote.runtime.agent.components.context_provider import ContextProvider
from mote.runtime.agent.role_state import RoleStateController
from mote.runtime.agent.session_manager import RoleSessionManager
from mote.runtime.artifacts import ArtifactRepositoryBlobStore, ArtifactRepositoryBundle
from mote.runtime.context import ContextManager, ContextVisibility
from mote.runtime.context.turn import TurnContextBus
from mote.runtime.events import EventFabric
from mote.runtime.events.backends import SQLiteSubscriptionStateStore
from mote.runtime.events.telemetry import TelemetryRuntime
from mote.runtime.fileops import FileOperations
from mote.runtime.hook.manager import HookManager
from mote.runtime.interactive import ArtifactCheckpointPayloadStore, RuntimeHost
from mote.runtime.interactive.browser.profile import BrowserProfileStore
from mote.runtime.lsp.service import LspService
from mote.runtime.models.gateway import LLMRouter
from mote.runtime.models.inference_port import RuntimeModelInferencePort
from mote.runtime.output.graph_service import GraphOutputService
from mote.runtime.projections import RuntimeProjectionReconciler, RuntimeProjectionRegistry, SessionLiveProjection
from mote.runtime.resources import ResourceRegistry
from mote.runtime.sandbox import SandboxRuntime
from mote.runtime.secrets.store import SecretStore
from mote.runtime.session import RuntimeCheckpointRecorder, SessionLog, SessionRuntimeProjectionJournal
from mote.runtime.session.committer import SessionFactCommitter
from mote.runtime.session.runtime_handoff import SessionRuntimeHandoffJournal
from mote.runtime.session.runtime_operation import SessionRuntimeOperationJournal
from mote.runtime.session.subscribers import CheckpointSubscriber, TitleSubscriber
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.tools.snapshots import RuntimeToolSnapshotManager
from mote.runtime.tools.tool_executor import ToolExecutor
from mote.runtime.watching import FileWatchService

WORKSPACE_STORE: ComponentKey[SessionWorkspace] = ComponentKey("workspace_store")
BACKGROUND_POOL: ComponentKey[BackgroundTaskService] = ComponentKey("bg_pool")
TOOL_CALL_POLICY: ComponentKey[ToolCallPolicy] = ComponentKey("tool_call_policy")
TOOL_RESULT_POLICY: ComponentKey[ToolResultPolicy] = ComponentKey("tool_result_policy")
EXECUTOR: ComponentKey[ToolExecutor[object]] = ComponentKey("executor")
COMMAND_CHANNEL: ComponentKey[CommandChannel] = ComponentKey("command_channel")
GRAPH_OUTPUT_SERVICE: ComponentKey[GraphOutputService] = ComponentKey("graph_output_service")
BROWSER_PROFILE_STORE: ComponentKey[BrowserProfileStore] = ComponentKey("browser_profile_store")
ARTIFACT_RESOLVER: ComponentKey[ArtifactResolver] = ComponentKey("artifact_resolver")
SESSION_FACT_COMMITTER: ComponentKey[SessionFactCommitter] = ComponentKey("session_fact_committer")
HOOK_MANAGER: ComponentKey[HookManager | None] = ComponentKey("hook_manager")
SECRET_STORE: ComponentKey[SecretStore] = ComponentKey("secret_store")
SKILL_MANAGER: ComponentKey[SkillService] = ComponentKey("skill_manager")
TELEMETRY: ComponentKey[TelemetryRuntime] = ComponentKey("telemetry")
STATE_CTL: ComponentKey[RoleStateController] = ComponentKey("state_ctl")
CAPABILITIES: ComponentKey[RoleCapabilities] = ComponentKey("capabilities")
SESSION_MANAGER: ComponentKey[RoleSessionManager] = ComponentKey("session_manager")
RUNTIME_HOST: ComponentKey[RuntimeHost] = ComponentKey("runtime_host")
ROUTER: ComponentKey[LLMRouter] = ComponentKey("router")
INFERENCE_PORT: ComponentKey[RuntimeModelInferencePort] = ComponentKey("inference_port")
TOOL_SNAPSHOT_MANAGER: ComponentKey[RuntimeToolSnapshotManager] = ComponentKey("tool_snapshot_manager")
CONTEXT_PROVIDER: ComponentKey[ContextProvider] = ComponentKey("context_provider")
INFERENCE_ENGINE_FACTORY: ComponentKey[Callable[[], BaseInferenceEngine]] = ComponentKey("inference_engine_factory")
INFERENCE_SUBSYSTEMS_FACTORY: ComponentKey[Callable[[], InferenceSubsystems]] = ComponentKey(
    "inference_subsystems_factory"
)
PROMPT_POLICY: ComponentKey[PromptPolicy] = ComponentKey("prompt_policy")
RUN_COMPLETION_POLICY: ComponentKey[RunCompletionPolicy] = ComponentKey("run_completion_policy")
CONTEXT_MANAGER: ComponentKey[ContextManager] = ComponentKey("context_manager")
CONTEXT_VISIBILITY: ComponentKey[ContextVisibility] = ComponentKey("context_visibility")
RESOURCE_REGISTRY: ComponentKey[ResourceRegistry] = ComponentKey("resource_registry")
REPO_INDEX: ComponentKey[CodeMapIndexer | None] = ComponentKey("repo_index")
TURN_CONTEXT_SOURCES: ComponentKey[list[EphemeralContextSource]] = ComponentKey("turn_context_sources")
TURN_CONTEXT_BUS: ComponentKey[TurnContextBus] = ComponentKey("turn_context_bus")
LSP_SERVICE: ComponentKey[LspService | None] = ComponentKey("lsp_service")
DIAGNOSTICS_BUFFER: ComponentKey[DiagnosticsProvider | None] = ComponentKey("diagnostics_buffer")
SANDBOX_RUNTIME: ComponentKey[SandboxRuntime | None] = ComponentKey("sandbox_runtime")
FILE_WATCH_SERVICE: ComponentKey[FileWatchService | None] = ComponentKey("file_watch_service")
SESSION_LOG: ComponentKey[SessionLog] = ComponentKey("session_log")
SESSION_PROJECTION: ComponentKey[SessionLiveProjection] = ComponentKey("session_projection")
SUBSCRIPTION_STATE_STORE: ComponentKey[SQLiteSubscriptionStateStore] = ComponentKey("subscription_state_store")
EVENT_FABRIC: ComponentKey[EventFabric] = ComponentKey("event_fabric")
FILE_OPERATIONS: ComponentKey[FileOperations] = ComponentKey("file_operations")
ARTIFACT_REPOSITORY_BUNDLE: ComponentKey[ArtifactRepositoryBundle] = ComponentKey("artifact_repository_bundle")
ARTIFACT_BLOB_STORE: ComponentKey[ArtifactRepositoryBlobStore] = ComponentKey("artifact_blob_store")
ARTIFACT_STORE: ComponentKey[ArtifactStore] = ComponentKey("artifact_store")
ARTIFACT_PUBLISHER: ComponentKey[ArtifactPublisherPort] = ComponentKey("artifact_publisher")
CHECKPOINT_PAYLOAD_STORE: ComponentKey[ArtifactCheckpointPayloadStore] = ComponentKey("checkpoint_payload_store")
RUNTIME_PROJECTION_JOURNAL: ComponentKey[SessionRuntimeProjectionJournal] = ComponentKey("runtime_projection_journal")
RUNTIME_HANDOFF_JOURNAL: ComponentKey[SessionRuntimeHandoffJournal] = ComponentKey("runtime_handoff_journal")
RUNTIME_OPERATION_JOURNAL: ComponentKey[SessionRuntimeOperationJournal] = ComponentKey("runtime_operation_journal")
RUNTIME_PROJECTION_REGISTRY: ComponentKey[RuntimeProjectionRegistry] = ComponentKey("runtime_projection_registry")
RUNTIME_PROJECTION_RECONCILER: ComponentKey[RuntimeProjectionReconciler] = ComponentKey("runtime_projection_reconciler")
CHECKPOINT_SUBSCRIBER: ComponentKey[CheckpointSubscriber | None] = ComponentKey("checkpoint_subscriber")
TITLE_SUBSCRIBER: ComponentKey[TitleSubscriber | None] = ComponentKey("title_subscriber")
RUNTIME_CHECKPOINT_RECORDER: ComponentKey[RuntimeCheckpointRecorder] = ComponentKey("runtime_checkpoint_recorder")

__all__ = [
    "ARTIFACT_BLOB_STORE",
    "ARTIFACT_PUBLISHER",
    "ARTIFACT_REPOSITORY_BUNDLE",
    "ARTIFACT_RESOLVER",
    "ARTIFACT_STORE",
    "BACKGROUND_POOL",
    "BROWSER_PROFILE_STORE",
    "CAPABILITIES",
    "CHECKPOINT_PAYLOAD_STORE",
    "CHECKPOINT_SUBSCRIBER",
    "COMMAND_CHANNEL",
    "CONTEXT_MANAGER",
    "CONTEXT_PROVIDER",
    "CONTEXT_VISIBILITY",
    "DIAGNOSTICS_BUFFER",
    "EVENT_FABRIC",
    "EXECUTOR",
    "FILE_OPERATIONS",
    "FILE_WATCH_SERVICE",
    "GRAPH_OUTPUT_SERVICE",
    "HOOK_MANAGER",
    "INFERENCE_ENGINE_FACTORY",
    "INFERENCE_PORT",
    "INFERENCE_SUBSYSTEMS_FACTORY",
    "LSP_SERVICE",
    "PROMPT_POLICY",
    "REPO_INDEX",
    "RESOURCE_REGISTRY",
    "ROUTER",
    "RUN_COMPLETION_POLICY",
    "RUNTIME_CHECKPOINT_RECORDER",
    "RUNTIME_HANDOFF_JOURNAL",
    "RUNTIME_HOST",
    "RUNTIME_OPERATION_JOURNAL",
    "RUNTIME_PROJECTION_JOURNAL",
    "RUNTIME_PROJECTION_RECONCILER",
    "RUNTIME_PROJECTION_REGISTRY",
    "SANDBOX_RUNTIME",
    "SECRET_STORE",
    "SESSION_FACT_COMMITTER",
    "SESSION_LOG",
    "SESSION_MANAGER",
    "SESSION_PROJECTION",
    "SKILL_MANAGER",
    "STATE_CTL",
    "SUBSCRIPTION_STATE_STORE",
    "TELEMETRY",
    "TITLE_SUBSCRIBER",
    "TOOL_CALL_POLICY",
    "TOOL_RESULT_POLICY",
    "TOOL_SNAPSHOT_MANAGER",
    "TURN_CONTEXT_BUS",
    "TURN_CONTEXT_SOURCES",
    "WORKSPACE_STORE",
]
