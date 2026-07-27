"""Typed public projections over the Role component graph."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from mote.contracts.background_tasks import BackgroundTaskService
from mote.kernel.flow import AgentFlowEngine
from mote.kernel.think.prompt_builder import ThinkSubsystems
from mote.runtime.agent.component_graph import ComponentGraph
from mote.runtime.agent.context_provider import ContextProvider
from mote.runtime.agent.graph_output_service import GraphOutputService
from mote.runtime.context import ContextManager, ContextVisibility
from mote.runtime.context.skills.skill_manager import SkillManager
from mote.runtime.models.gateway import LLMRouter

if TYPE_CHECKING:
    from mote.contracts.ports import (
        ArtifactResolver,
        ArtifactStore,
        PromptPolicy,
        ReliableArtifactPublisher,
        RunCompletionPolicy,
        ToolCallPolicy,
        ToolResultPolicy,
    )
    from mote.kernel.parser import CommandChannel
    from mote.runtime.agent.capabilities import RoleCapabilities
    from mote.runtime.agent.role_state import RoleStateController
    from mote.runtime.agent.session_manager import RoleSessionManager
    from mote.runtime.events import EventFabric
    from mote.runtime.events.backends import SQLiteSubscriptionStateStore
    from mote.runtime.fileops import FileOperations
    from mote.runtime.interactive import RuntimeHost
    from mote.runtime.projections import SessionLiveProjection
    from mote.runtime.resources import ResourceRegistry
    from mote.runtime.session import SessionLog
    from mote.runtime.session.subscribers import CheckpointSubscriber, TitleSubscriber
    from mote.runtime.tools.dependency.browser_profile import BrowserProfileStore
    from mote.runtime.tools.tool_executor import ToolExecutor


class RoleComponentAccessors:
    """Stable Role-facing API; implementations are graph key projections."""

    _graph: ComponentGraph

    def _get(self, name: str):
        return self._graph.get(name)

    @property
    def state_ctl(self) -> "RoleStateController":
        return self._get("state_ctl")

    @property
    def capabilities(self) -> "RoleCapabilities":
        return self._get("capabilities")

    @property
    def session_manager(self) -> "RoleSessionManager":
        return self._get("session_manager")

    @property
    def runtime_host(self) -> "RuntimeHost":
        return self._get("runtime_host")

    @property
    def router(self) -> LLMRouter:
        return self._get("router")

    @property
    def skill_manager(self) -> SkillManager:
        return self._get("skill_manager")

    @property
    def bg_pool(self) -> BackgroundTaskService:
        return self._get("bg_pool")

    @property
    def executor(self) -> "ToolExecutor":
        return self._get("executor")

    @property
    def tool_call_policy(self) -> "ToolCallPolicy":
        return self._get("tool_call_policy")

    @property
    def tool_result_policy(self) -> "ToolResultPolicy":
        return self._get("tool_result_policy")

    @property
    def prompt_policy(self) -> "PromptPolicy":
        return self._get("prompt_policy")

    @property
    def run_completion_policy(self) -> "RunCompletionPolicy":
        return self._get("run_completion_policy")

    @property
    def graph_output_service(self) -> GraphOutputService:
        return self._get("graph_output_service")

    @property
    def context_manager(self) -> ContextManager:
        return self._get("context_manager")

    @property
    def context_visibility(self) -> ContextVisibility:
        return self._get("context_visibility")

    @property
    def resource_registry(self) -> "ResourceRegistry":
        return self._get("resource_registry")

    @property
    def session_log(self) -> "SessionLog":
        return self._get("session_log")

    @property
    def session_projection(self) -> "SessionLiveProjection":
        return self._get("session_projection")

    @property
    def subscription_state_store(self) -> "SQLiteSubscriptionStateStore":
        return self._get("subscription_state_store")

    @property
    def event_fabric(self) -> "EventFabric":
        return self._get("event_fabric")

    @property
    def session_fact_committer(self):
        return self._get("session_fact_committer")

    @property
    def telemetry(self):
        return self._get("telemetry")

    @property
    def file_operations(self) -> "FileOperations":
        return self._get("file_operations")

    @property
    def artifact_store(self) -> "ArtifactStore":
        return self._get("artifact_store")

    @property
    def artifact_resolver(self) -> "ArtifactResolver":
        return self._get("artifact_resolver")

    @property
    def artifact_publisher(self) -> "ReliableArtifactPublisher":
        return self._get("artifact_publisher")

    @property
    def checkpoint_subscriber(self) -> "Optional[CheckpointSubscriber]":
        return self._get("checkpoint_subscriber")

    @property
    def title_subscriber(self) -> "Optional[TitleSubscriber]":
        return self._get("title_subscriber")

    @property
    def browser_profile_store(self) -> "BrowserProfileStore":
        return self._get("browser_profile_store")

    @property
    def hook_manager(self):
        return self._get("hook_manager")

    @property
    def lsp_service(self):
        return self._get("lsp_service")

    @property
    def sandbox_runtime(self):
        return self._get("sandbox_runtime")

    @property
    def secret_store(self):
        return self._get("secret_store")

    @property
    def repo_index(self):
        return self._get("repo_index")

    @property
    def diagnostics_buffer(self):
        return self._get("diagnostics_buffer")

    @property
    def file_watch_service(self):
        return self._get("file_watch_service")

    @property
    def turn_context_sources(self) -> list:
        return self._get("turn_context_sources")

    @property
    def turn_context_bus(self):
        return self._get("turn_context_bus")

    @property
    def command_channel(self) -> "CommandChannel":
        return self._get("command_channel")

    @property
    def context_provider(self) -> ContextProvider:
        return self._get("context_provider")

    def make_flow_engine(self) -> AgentFlowEngine:
        return self._get("flow_engine_factory")()

    def make_think_subsystems(self) -> ThinkSubsystems:
        return self._get("think_subsystems_factory")()

    def peek_bg_pool(self) -> "Optional[BackgroundTaskService]":
        return self._graph.peek("bg_pool")

    def peek_telemetry(self):
        return self._graph.peek("telemetry")

    def peek_event_fabric(self):
        return self._graph.peek("event_fabric")

    def peek_executor(self):
        return self._graph.peek("executor")

    def peek_runtime_host(self):
        return self._graph.peek("runtime_host")

    def peek_lsp_service(self):
        return self._graph.peek("lsp_service")

    def peek_sandbox_runtime(self):
        return self._graph.peek("sandbox_runtime")

    def peek_repo_index(self):
        return self._graph.peek("repo_index")

    def peek_file_watch_service(self):
        return self._graph.peek("file_watch_service")

    def peek_title_subscriber(self):
        return self._graph.peek("title_subscriber")
