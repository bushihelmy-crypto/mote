"""Typed public projections over the Role component graph."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from mote.context import ContextManager, ContextVisibility
from mote.context.skills.skill_manager import SkillManager
from mote.loop import BaseLoop
from mote.roles.component_graph import ComponentGraph
from mote.roles.context_provider import ContextProvider
from mote.router.router import LLMRouter
from mote.think.prompt_builder import ThinkSubsystems

if TYPE_CHECKING:
    from mote.common.resource import ResourceRegistry
    from mote.executor.dependency.browser_profile import BrowserProfileStore
    from mote.executor.tasks import BackgroundTaskPool
    from mote.executor.tool_executor import ToolExecutor
    from mote.parser import CommandChannel
    from mote.roles.capabilities import RoleCapabilities
    from mote.roles.role_state import RoleStateController
    from mote.roles.session_manager import RoleSessionManager
    from mote.session import (
        BrowserStateRecorder,
        FileSnapshotRecorder,
        HunkLedger,
        KernelStateRecorder,
        SessionLog,
        TerminalStateRecorder,
    )
    from mote.session.subscribers import CheckpointSubscriber, HunkSubscriber, TitleSubscriber


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
    def router(self) -> LLMRouter:
        return self._get("router")

    @property
    def skill_manager(self) -> SkillManager:
        return self._get("skill_manager")

    @property
    def bg_pool(self) -> "BackgroundTaskPool":
        return self._get("bg_pool")

    @property
    def executor(self) -> "ToolExecutor":
        return self._get("executor")

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
    def event_bus(self):
        return self._get("event_bus")

    @property
    def file_snapshot_recorder(self) -> "FileSnapshotRecorder":
        return self._get("file_snapshot_recorder")

    @property
    def checkpoint_subscriber(self) -> "Optional[CheckpointSubscriber]":
        return self._get("checkpoint_subscriber")

    @property
    def title_subscriber(self) -> "Optional[TitleSubscriber]":
        return self._get("title_subscriber")

    @property
    def hunk_ledger(self) -> "HunkLedger":
        return self._get("hunk_ledger")

    @property
    def hunk_subscriber(self) -> "Optional[HunkSubscriber]":
        return self._get("hunk_subscriber")

    @property
    def terminal_state_recorder(self) -> "TerminalStateRecorder":
        return self._get("terminal_state_recorder")

    @property
    def kernel_state_recorder(self) -> "KernelStateRecorder":
        return self._get("kernel_state_recorder")

    @property
    def browser_state_recorder(self) -> "BrowserStateRecorder":
        return self._get("browser_state_recorder")

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

    def make_loop(self) -> BaseLoop:
        return self._get("loop_factory")()

    def make_think_subsystems(self) -> ThinkSubsystems:
        return self._get("think_subsystems_factory")()

    def peek_bg_pool(self) -> "Optional[BackgroundTaskPool]":
        return self._graph.peek("bg_pool")

    def peek_event_bus(self):
        return self._graph.peek("event_bus")

    def peek_executor(self):
        return self._graph.peek("executor")

    def peek_lsp_service(self):
        return self._graph.peek("lsp_service")

    def peek_sandbox_runtime(self):
        return self._graph.peek("sandbox_runtime")

    def peek_repo_index(self):
        return self._graph.peek("repo_index")

    def peek_file_watch_service(self):
        return self._graph.peek("file_watch_service")
