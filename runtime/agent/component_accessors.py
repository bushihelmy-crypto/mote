"""Typed public projections over the Role component graph."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Generic, Optional, TypeVar

from mote.contracts.ports.skill.registry import SkillService
from mote.contracts.ports.task.operations import BackgroundTaskService
from mote.kernel.execution import ExecutionEngine
from mote.kernel.inference.prompt_builder import InferenceSubsystems
from mote.runtime.agent.component_graph import ComponentGraph, ComponentKey
from mote.runtime.agent.component_keys import (
    ARTIFACT_PUBLISHER,
    ARTIFACT_RESOLVER,
    ARTIFACT_STORE,
    BACKGROUND_POOL,
    BROWSER_PROFILE_STORE,
    CAPABILITIES,
    CHECKPOINT_SUBSCRIBER,
    COMMAND_CHANNEL,
    CONTEXT_MANAGER,
    CONTEXT_PROVIDER,
    CONTEXT_VISIBILITY,
    DIAGNOSTICS_BUFFER,
    EVENT_FABRIC,
    EXECUTOR,
    FILE_OPERATIONS,
    FILE_WATCH_SERVICE,
    GRAPH_OUTPUT_SERVICE,
    HOOK_MANAGER,
    INFERENCE_SUBSYSTEMS_FACTORY,
    LSP_SERVICE,
    PROMPT_POLICY,
    REPO_INDEX,
    RESOURCE_REGISTRY,
    ROUTER,
    RUN_COMPLETION_POLICY,
    RUNTIME_HOST,
    SANDBOX_RUNTIME,
    SECRET_STORE,
    SESSION_FACT_COMMITTER,
    SESSION_LOG,
    SESSION_MANAGER,
    SESSION_PROJECTION,
    SKILL_MANAGER,
    STATE_CTL,
    SUBSCRIPTION_STATE_STORE,
    TELEMETRY,
    TITLE_SUBSCRIBER,
    TOOL_CALL_POLICY,
    TOOL_RESULT_POLICY,
    TURN_CONTEXT_BUS,
    TURN_CONTEXT_SOURCES,
    WORKSPACE_STORE,
)
from mote.runtime.agent.components.context_provider import ContextProvider
from mote.runtime.context import ContextManager, ContextVisibility
from mote.runtime.models.gateway import LLMRouter
from mote.runtime.output.graph_service import GraphOutputService

if TYPE_CHECKING:
    from mote.contracts.ports.artifact.store import (
        ArtifactRepositoryService,
        ArtifactResolver,
        ReliableArtifactPublisher,
    )
    from mote.contracts.ports.conversation.prompt_policy import PromptPolicy
    from mote.contracts.ports.output.run_completion_policy import RunCompletionPolicy
    from mote.contracts.ports.tool.policy import ToolCallPolicy, ToolResultPolicy
    from mote.kernel.commands import CommandChannel
    from mote.runtime.agent.capabilities import RoleCapabilities
    from mote.runtime.agent.role_state import RoleStateController
    from mote.runtime.agent.session_manager import RoleSessionManager
    from mote.runtime.events.backends import SQLiteSubscriptionStateStore
    from mote.runtime.events.fabric import EventFabric
    from mote.runtime.fileops import FileOperations
    from mote.runtime.interactive.browser.profile import BrowserProfileStore
    from mote.runtime.interactive.host import RuntimeHost
    from mote.runtime.resources import ResourceRegistry
    from mote.runtime.session import SessionLog
    from mote.runtime.session.projection import SessionLiveProjection
    from mote.runtime.session.subscribers import CheckpointSubscriber, TitleSubscriber
    from mote.runtime.session.workspace import SessionWorkspace
    from mote.runtime.tools.tool_executor import ToolExecutor


OutputT = TypeVar("OutputT")


class RoleComponentAccessors(Generic[OutputT]):
    """Stable Role-facing API; implementations are graph key projections."""

    _graph: ComponentGraph
    _execution_engine_factory_key: ComponentKey[Callable[[], ExecutionEngine[OutputT]]]

    @property
    def state_ctl(self) -> "RoleStateController":
        return self._graph.get(STATE_CTL)

    @property
    def capabilities(self) -> "RoleCapabilities":
        return self._graph.get(CAPABILITIES)

    @property
    def session_manager(self) -> "RoleSessionManager":
        return self._graph.get(SESSION_MANAGER)

    @property
    def runtime_host(self) -> "RuntimeHost":
        return self._graph.get(RUNTIME_HOST)

    @property
    def router(self) -> LLMRouter:
        return self._graph.get(ROUTER)

    @property
    def skill_manager(self) -> SkillService:
        return self._graph.get(SKILL_MANAGER)

    @property
    def bg_pool(self) -> BackgroundTaskService:
        return self._graph.get(BACKGROUND_POOL)

    @property
    def executor(self) -> "ToolExecutor":
        return self._graph.get(EXECUTOR)

    @property
    def tool_call_policy(self) -> "ToolCallPolicy":
        return self._graph.get(TOOL_CALL_POLICY)

    @property
    def tool_result_policy(self) -> "ToolResultPolicy":
        return self._graph.get(TOOL_RESULT_POLICY)

    @property
    def prompt_policy(self) -> "PromptPolicy":
        return self._graph.get(PROMPT_POLICY)

    @property
    def run_completion_policy(self) -> "RunCompletionPolicy":
        return self._graph.get(RUN_COMPLETION_POLICY)

    @property
    def graph_output_service(self) -> GraphOutputService:
        return self._graph.get(GRAPH_OUTPUT_SERVICE)

    @property
    def context_manager(self) -> ContextManager:
        return self._graph.get(CONTEXT_MANAGER)

    @property
    def context_visibility(self) -> ContextVisibility:
        return self._graph.get(CONTEXT_VISIBILITY)

    @property
    def resource_registry(self) -> "ResourceRegistry":
        return self._graph.get(RESOURCE_REGISTRY)

    @property
    def session_log(self) -> "SessionLog":
        return self._graph.get(SESSION_LOG)

    @property
    def workspace_store(self) -> "SessionWorkspace":
        return self._graph.get(WORKSPACE_STORE)

    @property
    def session_projection(self) -> "SessionLiveProjection":
        return self._graph.get(SESSION_PROJECTION)

    @property
    def subscription_state_store(self) -> "SQLiteSubscriptionStateStore":
        return self._graph.get(SUBSCRIPTION_STATE_STORE)

    @property
    def event_fabric(self) -> "EventFabric":
        return self._graph.get(EVENT_FABRIC)

    @property
    def session_fact_committer(self):
        return self._graph.get(SESSION_FACT_COMMITTER)

    @property
    def telemetry(self):
        return self._graph.get(TELEMETRY)

    @property
    def file_operations(self) -> "FileOperations":
        return self._graph.get(FILE_OPERATIONS)

    @property
    def artifact_store(self) -> "ArtifactRepositoryService":
        return self._graph.get(ARTIFACT_STORE)

    @property
    def artifact_resolver(self) -> "ArtifactResolver":
        return self._graph.get(ARTIFACT_RESOLVER)

    @property
    def artifact_publisher(self) -> "ReliableArtifactPublisher":
        return self._graph.get(ARTIFACT_PUBLISHER)

    @property
    def checkpoint_subscriber(self) -> "Optional[CheckpointSubscriber]":
        return self._graph.get(CHECKPOINT_SUBSCRIBER)

    @property
    def title_subscriber(self) -> "Optional[TitleSubscriber]":
        return self._graph.get(TITLE_SUBSCRIBER)

    @property
    def browser_profile_store(self) -> "BrowserProfileStore":
        return self._graph.get(BROWSER_PROFILE_STORE)

    @property
    def hook_manager(self):
        return self._graph.get(HOOK_MANAGER)

    @property
    def lsp_service(self):
        return self._graph.get(LSP_SERVICE)

    @property
    def sandbox_runtime(self):
        return self._graph.get(SANDBOX_RUNTIME)

    @property
    def secret_store(self):
        return self._graph.get(SECRET_STORE)

    @property
    def repo_index(self):
        return self._graph.get(REPO_INDEX)

    @property
    def diagnostics_buffer(self):
        return self._graph.get(DIAGNOSTICS_BUFFER)

    @property
    def file_watch_service(self):
        return self._graph.get(FILE_WATCH_SERVICE)

    @property
    def turn_context_sources(self) -> list:
        return self._graph.get(TURN_CONTEXT_SOURCES)

    @property
    def turn_context_bus(self):
        return self._graph.get(TURN_CONTEXT_BUS)

    @property
    def command_channel(self) -> "CommandChannel":
        return self._graph.get(COMMAND_CHANNEL)

    @property
    def context_provider(self) -> ContextProvider:
        return self._graph.get(CONTEXT_PROVIDER)

    def make_flow_engine(self) -> ExecutionEngine[OutputT]:
        return self._graph.get(self._execution_engine_factory_key)()

    def make_think_subsystems(self) -> InferenceSubsystems:
        return self._graph.get(INFERENCE_SUBSYSTEMS_FACTORY)()

    def peek_bg_pool(self) -> "Optional[BackgroundTaskService]":
        return self._graph.peek(BACKGROUND_POOL)

    def peek_telemetry(self):
        return self._graph.peek(TELEMETRY)

    def peek_event_fabric(self):
        return self._graph.peek(EVENT_FABRIC)

    def peek_session_log(self):
        return self._graph.peek(SESSION_LOG)

    def peek_executor(self):
        return self._graph.peek(EXECUTOR)

    def peek_runtime_host(self):
        return self._graph.peek(RUNTIME_HOST)

    def peek_lsp_service(self):
        return self._graph.peek(LSP_SERVICE)

    def peek_sandbox_runtime(self):
        return self._graph.peek(SANDBOX_RUNTIME)

    def peek_repo_index(self):
        return self._graph.peek(REPO_INDEX)

    def peek_file_watch_service(self):
        return self._graph.peek(FILE_WATCH_SERVICE)

    def peek_title_subscriber(self):
        return self._graph.peek(TITLE_SUBSCRIBER)
