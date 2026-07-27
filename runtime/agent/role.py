#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Generic, Optional, Set, TypeVar, cast
from uuid import uuid4

from mote.contracts.background_tasks import BackgroundTaskService
from mote.contracts.constants.messages import MESSAGE_ROUTE_TO_SELF
from mote.contracts.output import RunOutcome, RunRejected, RunRejectionKind, RunResult, TranscriptRef
from mote.contracts.policy.prompt import PromptIntent
from mote.contracts.policy.run_completion import RunCompletionDecision, RunCompletionIntent
from mote.contracts.run_context import RunContext
from mote.contracts.schema import AIMessage, CauseBy, Message
from mote.contracts.services import ServiceExecutionSemantics, ServiceInvocation
from mote.kernel.models.model_calls import describe_image as describe_image_with_model
from mote.kernel.parser import CommandChannel
from mote.kernel.tools.toolset import toolset_manifest, validate_toolset_protocols
from mote.runtime.agent.base import BaseRole
from mote.runtime.agent.context_provider import ContextProvider
from mote.runtime.agent.execution import any_to_str, role_raise_decorator
from mote.runtime.agent.incarnation import AgentIncarnationBlueprint
from mote.runtime.agent.role_components import RoleComponents
from mote.runtime.agent.role_schema import RoleSchema
from mote.runtime.agent.role_state import RoleState
from mote.runtime.agent.wiring import AgentWiring
from mote.runtime.context import ContextManager
from mote.runtime.context.skills.skill_manager import SkillManager
from mote.runtime.errors import RoleContextNotSetError, ToolNotConfiguredError
from mote.runtime.events import (
    OutputPublicationQueuedEvent,
    OutputPublishedEvent,
    PromptRejectedEvent,
    SessionStartEvent,
    TurnEndEvent,
    UserPromptSubmitEvent,
    bind_telemetry,
    span,
)
from mote.runtime.lifecycle import LifecyclePhase, LifecycleStack
from mote.runtime.logging import bind_session_logfile, bind_trace, log_class, logger, unbind_session_logfile
from mote.runtime.models.gateway import LLMRouter
from mote.runtime.paths import CONFIG_ROOT
from mote.runtime.run_context import bind_run_context
from mote.runtime.services import EngineServices
from mote.runtime.session import list_sessions as _list
from mote.runtime.session.attribution import HunkAttribution
from mote.runtime.session.events import SessionMetaEvent
from mote.runtime.session.hunk_ops import HunkOps
from mote.runtime.tools.execution_context import current_tool_call_id
from mote.runtime.tools.tool_executor import ToolExecutor

DepsT = TypeVar("DepsT")
OutputT = TypeVar("OutputT")
AgentT = TypeVar("AgentT")

if TYPE_CHECKING:
    from mote.contracts.artifacts import ArtifactRef
    from mote.contracts.interaction import AskUserQuestionAnswers, AskUserQuestionItem
    from mote.contracts.permissions import ApprovalChoice, ApprovalRequest
    from mote.contracts.ports import (
        ArtifactResolver,
        ArtifactStore,
        PromptPolicy,
        ReliableArtifactPublisher,
        RunCompletionPolicy,
        ToolCallPolicy,
        ToolResultPolicy,
    )
    from mote.contracts.settings.device import DeviceConfig
    from mote.runtime.context.skills.skill_pool import SkillPool
    from mote.runtime.sandbox import SandboxRuntime
    from mote.runtime.session import SessionLog
    from mote.runtime.tools.capability_types import CapabilityMap
    from mote.runtime.tools.dependency.browser_profile import BrowserProfileStore
    from mote.runtime.tools.tool_result import ToolResult


@log_class(
    level="DEBUG",
    exclude={
        # Hot / trivial accessors and signal setters — wrapping them only adds
        # noise. `run` is excluded here and traced explicitly below (bind_trace).
        "run",
        "get_cwd",
        "set_cwd",
        "is_resource_visible",
        "put_message",
        "publish_message",
        "tool_capabilities",
        "deactivate",
        "get_memories",
        "set_env",
        "set_addresses",
    },
)
class Role(BaseRole, Generic[DepsT, OutputT]):
    """Unified Role/Agent — pure orchestration via composition.

    Composes:
      - role_schema: RoleSchema (static config, deploy-time)
      - state: RoleState (runtime snapshot, serializable for checkpoint/recovery)
      - Lazy-init components: ThinkEngine, ToolExecutor, SkillManager, etc.

    Not a Pydantic BaseModel: construction is explicit via __init__.
    Serialization is handled by dump()/load() which delegate to RoleState (Pydantic).
    """

    role_type_id = "mote.agent.role.v1"
    legacy_role_type_ids = ("mote.roles.role.Role",)

    def __init__(
        self,
        *,
        wiring: AgentWiring[DepsT, OutputT] | None = None,
        config=None,
        name: Optional[str] = None,
        role_schema: Optional[RoleSchema] = None,
        state: Optional[RoleState] = None,
        **schema_kwargs,
    ):
        # Static config
        if role_schema is not None:
            self.role_schema = role_schema
        elif schema_kwargs:
            self.role_schema = RoleSchema(**schema_kwargs)
        else:
            self.role_schema = RoleSchema()

        if name is not None:
            self.role_schema.name = name

        # Runtime state
        self.state = state if state is not None else RoleState()

        # External dependencies (injected)
        self.wiring: AgentWiring[DepsT, OutputT] = (
            wiring if wiring is not None else cast(AgentWiring[DepsT, OutputT], AgentWiring.defaults())
        )
        validate_toolset_protocols(
            self.role_schema.command_protocol,
            self.wiring.dependencies.toolsets,
        )
        self._config = config

        # Lazy assembly + ownership of all subsystems (router, executor, context
        # manager, Telemetry, session log, hook/LSP/file-watch services, the
        # per-turn context bus, …) — including the two behaviour holders (the
        # state controller and the tool-capabilities holder). The Role keeps a
        # thin property surface that delegates onto this holder; the wiring logic
        # lives there. Role.__init__ constructs nothing but this holder.
        self._components = RoleComponents(self)
        self._cleanup_task: asyncio.Task[None] | None = None
        self._cleanup_complete = False
        self._cleanup_lifecycle = LifecycleStack()
        self._cleanup_lifecycle_prepared = False
        self.agent_control = None
        # Guards firing SessionStart exactly once across this Role's run() calls.
        self._session_started = False

        # Post-init
        self._init_addresses()

    def __hash__(self):
        return id(self)

    # =========================================================================
    # Serialization — delegates to RoleState (Pydantic) + class registry
    # =========================================================================

    def dump(self) -> dict[str, Any]:
        """Serialize role for checkpoint/recovery."""
        return {
            "type_id": self.role_type_id,
            "state": self.state.model_dump(mode="json"),
            "role_schema": self.role_schema.model_dump(mode="json"),
        }

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "Role":
        """Reconstruct a Role from serialized data."""
        schema_data = data.get("role_schema", {})
        state_data = data.get("state", {})
        role_schema = RoleSchema.model_validate(schema_data)
        state = RoleState.model_validate(state_data)
        return cls(role_schema=role_schema, state=state)

    # =========================================================================
    # Properties — context / config / llm delegation
    # =========================================================================

    @property
    def name(self) -> str:
        return self.role_schema.name

    @name.setter
    def name(self, value: str):
        self.role_schema.name = value

    @property
    def components(self) -> RoleComponents:
        """The Role's subsystem holder (lazy assembly + ownership). See
        :class:`RoleComponents`. The component properties below delegate here.
        """
        return self._components

    @property
    def _state_ctl(self):
        """Behaviour over the (pure-DTO) RoleState — resolved through the graph.

        The Role's state methods (cwd, file-read map, active signal, …) are thin
        delegators onto this controller; it lives in the component graph like
        every other collaborator so ``__init__`` builds nothing itself.
        """
        return self._components.state_ctl

    @property
    def _capabilities(self):
        """Subsystem-backed tool capabilities (human I/O, sleep, end-of-session
        summary, skill forks, the task/skill pools) — resolved through the graph.
        """
        return self._components.capabilities

    @property
    def _session_manager(self):
        """Session resume/fork behaviour (replay history, branch a sibling) —
        resolved through the graph. The Role keeps thin ``resume_session`` /
        ``fork_session`` delegators onto this holder.
        """
        return self._components.session_manager

    @property
    def router(self) -> LLMRouter:
        """The LLM router bound to this Role's context (delegates to components)."""
        return self._components.router

    @property
    def config(self):
        if self._config:
            return self._config
        return self.context.config

    @config.setter
    def config(self, config):
        self._config = config

    @property
    def context(self):
        if self.wiring.services is not None:
            return self.wiring.services.context
        raise RoleContextNotSetError("Role.context not set. Provide EngineServices through AgentWiring.")

    def bind_services(self, services: EngineServices, *, owned: bool = False) -> None:
        """Provision this Role with Engine-owned or Role-owned shared services."""

        if self.wiring.services is not None and self.wiring.services is not services:
            raise RuntimeError("Role EngineServices cannot be rebound after provisioning")
        self.wiring = self.wiring.with_services(services, owned=owned)

    @property
    def deps(self) -> DepsT:
        return self.wiring.dependencies.deps

    @property
    def output_contract(self):
        return self.wiring.dependencies.output_contract

    # =========================================================================
    # Component properties (lazy-init)
    # =========================================================================

    # Each property below is a thin delegator onto :class:`RoleComponents`,
    # which owns the slots + lazy construction. External callers and tests keep
    # using ``role.<component>``; the wiring lives in role_components.py.

    @property
    def skill_manager(self) -> SkillManager:
        return self._components.skill_manager

    @property
    def bg_pool(self) -> BackgroundTaskService:
        return self._components.bg_pool

    def _peek_bg_pool(self) -> Optional[BackgroundTaskService]:
        """Return the background pool only if a tool already created it (no build)."""
        return self._components.peek_bg_pool()

    def set_task_completion_wake(self, wake) -> None:
        """Wire a wake callback so bg-task completions trigger a new turn."""
        self._components.set_task_completion_wake(wake)

    @property
    def executor(self) -> ToolExecutor:
        return self._components.executor

    @property
    def tool_call_policy(self) -> "ToolCallPolicy":
        return self._components.tool_call_policy

    @property
    def tool_result_policy(self) -> "ToolResultPolicy":
        return self._components.tool_result_policy

    @property
    def prompt_policy(self) -> "PromptPolicy":
        return self._components.prompt_policy

    @property
    def run_completion_policy(self) -> "RunCompletionPolicy":
        return self._components.run_completion_policy

    @property
    def context_manager(self) -> ContextManager:
        return self._components.context_manager

    @property
    def session_log(self) -> "SessionLog":
        return self._components.session_log

    @property
    def telemetry(self):
        return self._components.telemetry

    @property
    def file_operations(self):
        return self._components.file_operations

    @property
    def artifact_store(self) -> "ArtifactStore":
        return self._components.artifact_store

    @property
    def artifact_resolver(self) -> "ArtifactResolver":
        return self._components.artifact_resolver

    @property
    def artifact_publisher(self) -> "ReliableArtifactPublisher":
        return self._components.artifact_publisher

    @property
    def review_service(self):
        """The rollout-backed durable hunk review service."""
        return self.file_operations.review

    @property
    def hunk_ops(self) -> "HunkOps":
        """Accept / reject / undo over rollout-backed review events."""
        ops = getattr(self, "_hunk_ops", None)
        if ops is None:
            ops = HunkOps(
                self.review_service,
                self.file_operations.artifacts,
                capture_snapshot=self.file_operations.capture,
                mutation_factory=self.file_operations.mutation_factory,
                commit_mutation_set=self.file_operations.mutations.commit,
                resource_lease=self.file_operations.mutations.lease,
            )
            self._hunk_ops = ops
        return ops

    @property
    def hunk_attribution(self) -> "HunkAttribution":
        """Read-side projection of durable review events for review UIs.

        Groups review records (by turn / file / source) and rehydrates
        each hunk's before/after text on demand from the blob store. Built once
        and cached — the ledger and blob store it binds to are stable for the
        Role's lifetime.
        """
        attr = getattr(self, "_hunk_attribution", None)
        if attr is None:
            attr = HunkAttribution(self.review_service, self.file_operations.artifacts)
            self._hunk_attribution = attr
        return attr

    @property
    def resource_registry(self):
        return self._components.resource_registry

    @property
    def runtime_host(self):
        return self._components.runtime_host

    @property
    def browser_profile_store(self) -> "BrowserProfileStore":
        return self._components.browser_profile_store

    @property
    def hook_manager(self):
        return self._components.hook_manager

    @property
    def lsp_service(self):
        return self._components.lsp_service

    @property
    def sandbox_runtime(self):
        return self._components.sandbox_runtime

    @property
    def diagnostics_buffer(self):
        return self._components.diagnostics_buffer

    def turn_context_source(self, name: str):
        """Look up a per-turn context feed by its ``name`` (or ``None``).

        A generic accessor over the single source roster, replacing the former
        per-feed properties (``compaction_notice`` etc.): adding a feed never
        needs a matching accessor here.
        """
        return next(
            (s for s in self._components.turn_context_sources if s.name == name),
            None,
        )

    @property
    def file_watch_service(self):
        return self._components.file_watch_service

    @property
    def turn_context_bus(self):
        return self._components.turn_context_bus

    def register_hook(self, event: str, fn, matcher: Optional[str] = None) -> None:
        """Register an in-process Python hook callback (delegates to components).

        Engages the hook layer even with no ``HookConfig`` declared. Register
        before ``run()`` so the executor / context manager pick up the manager.
        """
        self._components.register_hook(event, fn, matcher)

    def _report_think_result(self, result) -> None:
        """Publish this turn's think result to state (used by the flow).

        The loop calls this the moment the think task drains, so a tool running
        later in the same turn (e.g. ``end_session``) reads the fresh result off
        RoleState instead of the think-engine machinery — which lets the engine
        be a stateless per-turn factory built by the graph's loop factory.
        """
        self._state_ctl.set_last_think_result(result)

    @property
    def command_channel(self) -> CommandChannel:
        return self._components.command_channel

    @property
    def context_provider(self) -> ContextProvider:
        return self._components.context_provider

    # =========================================================================
    # Framework properties
    # =========================================================================

    @property
    def session_id(self) -> str:
        return self.state.session_id

    @property
    def env(self):
        return self.state.env

    @property
    def is_idle(self) -> bool:
        """A role is idle when its message buffer is empty."""
        return self._state_ctl.is_idle

    def set_env(self, env):
        """Set the environment this role belongs to and register addresses."""
        self.state.env = env
        if env:
            env.set_addresses(self, self.state.addresses)

    # =========================================================================
    # Initialization helpers
    # =========================================================================

    def _init_addresses(self):
        """Set default addresses and recovery state."""
        if not self.state.addresses:
            self.state.addresses = (
                {any_to_str(self), self.role_schema.name} if self.role_schema.name else {any_to_str(self)}
            )
        if self.state.latest_observed_msg:
            self.state.recovered = True

    # =========================================================================
    # Framework methods
    # =========================================================================

    def set_addresses(self, addresses: Set[str]):
        """Used to receive Messages with certain tags from the environment."""
        self.state.addresses = addresses
        if self.state.env:
            self.state.env.set_addresses(self, self.state.addresses)

    def get_cwd(self) -> str:
        """Current working directory.

        Capability surface for tools; the cwd fallback logic lives on the
        :class:`RoleStateController` (state ownership stays out of tools).
        """
        return self._state_ctl.get_cwd()

    def set_cwd(self, path: str) -> None:
        """Set the stable working directory (framework API for an explicit switch).

        Not called by the Bash tool — a `cd` inside a command does not drift
        the cwd (Codex-aligned: cwd is stable data). The capability for a
        deliberate directory-change entry point. Delegates to the state
        controller.
        """
        self._state_ctl.set_cwd(path)

    def current_turn_index(self) -> int:
        """The current turn (prompt) index used to attribute change hunks.

        Capability surface for the hunk-attribution path (executor settle and
        the read-before-write guard): each captured hunk is stamped with this
        value so the review layer can group pending changes by turn. Delegates
        to the state controller.
        """
        return self._state_ctl.current_turn_index()

    def _advance_turn(self) -> int:
        """Advance the turn index by one (injected into the react loop).

        Called once per think round so hunks captured during a turn carry a
        stable, monotonic index. Framework-internal (not a tool capability).
        """
        return self._state_ctl.advance_turn()

    def get_default_model(self) -> Optional[str]:
        """Name of the default (main think-loop) model, or None if unconfigured.

        Capability surface for media tools (Read / WebBrowser screenshot): the
        media a tool attaches rides the MAIN model's request, so a tool checks
        ``supports_vision`` / ``supports_pdf_input`` against this name to refuse
        up-front (ToolNotConfiguredError) rather than attach media the model
        silently cannot read.
        """
        return getattr(self.config.models.default, "model", None)

    def record_file_glimpsed(self, path: str) -> None:
        """Record that a file surfaced in a search result (Grep/Glob), un-read.

        The Grep/Glob tools call this for each file they matched so the code map
        can surface those files' structure (defines + intent) as a "which of
        these should I open" hint — without the file's body ever entering
        context. Kept separate from sealed Read snapshots: a glimpse carries no body
        and must not trip the read-before-write guard.
        """
        self._state_ctl.record_file_glimpsed(path)

    def is_resource_visible(self, path: str) -> bool:
        """Is the most-recent tool result read from `path` still present in context?

        Delegates to :class:`~mote.runtime.context.visibility.ContextVisibility`. A
        deduplicating read tool consults this before returning a "you already
        read this" stub: if the earlier result has been folded/erased by
        compaction the stub would strand the model with no content, so the tool
        must re-read instead. Read-only; never mutates history.
        """
        return self._components.context_visibility.is_resource_visible(path)

    def get_runtime_host(self):
        """Return this Role's managed interactive-runtime composition root."""
        return self._components.runtime_host

    def get_artifact_store(self) -> "ArtifactStore":
        """Return this Role's durable logical Artifact store."""
        return self._components.artifact_store

    def get_artifact_publisher(self) -> "ReliableArtifactPublisher":
        """Return this Role's staged, crash-reconcilable Artifact publisher."""
        return self._components.artifact_publisher

    def get_skill_pool(self) -> Optional[SkillPool]:
        """Return the live SkillPool, or None when skills are disabled.

        Capability surface for the ``Skill`` bridge tool; delegates to
        :class:`RoleCapabilities`.
        """
        return self._capabilities.get_skill_pool()

    def build_child_agent(self, agent_cls: type[AgentT], /, **kwargs: Any) -> AgentT:
        """Construct a child via the injected application composition root."""

        return self._capabilities.build_child_agent(agent_cls, **kwargs)

    async def run_skill_fork(self, **kwargs) -> str:
        """Run a ``context: fork`` skill inside a fresh, isolated child Role.

        Capability surface for the ``Skill`` bridge tool; delegates to
        :class:`RoleCapabilities` (which owns the child lifecycle, including
        cleanup).
        """
        return await self._capabilities.run_skill_fork(**kwargs)

    # =========================================================================
    # Narrow capabilities exposed to tools (injected via BaseTool.requires).
    # Tools call these instead of receiving RoleState/memory/env directly, so
    # role behavior stays in the Role and tools stay thin triggers.
    # =========================================================================

    def tool_capabilities(self) -> CapabilityMap:
        """The explicit allowlist of capabilities a tool may receive via bind().

        BaseTool.bind() resolves each name in a tool's `requires` against this
        map — and ONLY this map — so a tool can never reach RoleState, memory,
        or any Role attribute that is not deliberately published here. Adding a
        capability is an explicit decision; `getattr(role, ...)` is never used.
        """
        return {
            "get_cwd": self.get_cwd,
            "set_cwd": self.set_cwd,
            "get_default_model": self.get_default_model,
            "deactivate": self.deactivate,
            "ask_user": self.ask_user,
            "ask_user_question": self.ask_user_question,
            "get_bg_pool": self.get_bg_pool,
            "request_approval": self.request_approval,
            "reply_to_user": self.reply_to_user,
            "end_session": self.end_session,
            "capture_file_snapshot": self._capabilities.capture_file_snapshot,
            "observe_file_snapshot": self._capabilities.observe_file_snapshot,
            "read_file_view": self._capabilities.read_file_view,
            "search_files": self._capabilities.search_files,
            "plan_file_edit": self._capabilities.plan_file_edit,
            "commit_edit_plan": self._capabilities.commit_edit_plan,
            "commit_generated_files": self._capabilities.commit_generated_files,
            "record_file_glimpsed": self.record_file_glimpsed,
            "is_resource_visible": self.is_resource_visible,
            "get_browser_stealth": self.get_browser_stealth,
            "get_browser_locale": self.get_browser_locale,
            "get_browser_proxy": self.get_browser_proxy,
            "get_browser_cdp_endpoint": self.get_browser_cdp_endpoint,
            "get_browser_profile": self.get_browser_profile,
            "load_browser_profile": self.load_browser_profile,
            "save_browser_profile": self.save_browser_profile,
            "get_browser_client_certs": self.get_browser_client_certs,
            "get_secret": self.get_secret,
            "get_runtime_host": self.get_runtime_host,
            "get_artifact_store": self.get_artifact_store,
            "get_artifact_publisher": self.get_artifact_publisher,
            "handoff_runtime": self.handoff_runtime,
            "wait_interruptible": self.wait_interruptible,
            "get_skill_pool": self.get_skill_pool,
            "build_child_agent": self.build_child_agent,
            "run_skill_fork": self.run_skill_fork,
            "register_resource": self._capabilities.register_resource,
            "register_task_result": self._capabilities.register_task_result,
            "retire_task_result": self._capabilities.retire_task_result,
            "get_sandbox_runtime": self.get_sandbox_runtime,
            "get_device_config": self.get_device_config,
            "dispatch_tool": self.dispatch_tool,
            "list_tool_names": self.list_tool_names,
            "list_graph_tool_names": self.list_graph_tool_names,
            "list_graph_excluded_tool_names": self.list_graph_excluded_tool_names,
            "commit_graph_output": self.commit_graph_output,
            "resume_graph_output": self.resume_graph_output,
            "has_graph_output_restore": self._state_ctl.has_pending_graph_output_restore,
            "graph_run_lease": self.graph_run_lease,
            "list_deferred_tools": self.list_deferred_tools,
            "reveal_tools": self.reveal_tools,
            "describe_deferred_tools": self.describe_deferred_tools,
            "describe_image": self.describe_image,
            "invoke_service": self.invoke_service,
        }

    async def invoke_service(
        self,
        *,
        route_id: str,
        capability: str,
        operation_key: str,
        payload: dict[str, Any],
        semantics: ServiceExecutionSemantics,
    ) -> Any:
        """Invoke one hosted Tool capability under a stable per-call identity."""

        gateway = self.context.service_gateway
        if gateway is None or not gateway.supports_route(route_id, capability):
            raise ToolNotConfiguredError(f"Hosted Tool service route {route_id!r} is not configured.")
        tool_call_id = current_tool_call_id() or uuid4().hex
        identity = f"{self.session_id}\0{tool_call_id}\0{route_id}\0" f"{capability}\0{operation_key}"
        service_call_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        canonical_payload = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        idempotency_key = hashlib.sha256(f"{identity}\0{canonical_payload}".encode("utf-8")).hexdigest()
        resolved = await gateway.execute(
            ServiceInvocation(
                service_call_id=service_call_id,
                route_id=route_id,
                capability=capability,
                payload=payload,
                semantics=semantics,
                idempotency_key=idempotency_key,
            )
        )
        return resolved.response.value

    def deactivate(self) -> None:
        """Stop the react loop after the current step."""
        self._state_ctl.deactivate()

    def _is_active(self) -> bool:
        """Read the shared active signal (consumed by the flow's think step)."""
        return self._state_ctl.is_active()

    def _set_active(self, value: bool) -> None:
        """Write the shared active signal (used by the flow each iteration).

        `active` lives on RoleState — not inside the engine — because it doubles
        as a tool→loop kill switch: the End tool and ask_user's "stop" call
        deactivate(), which must still break a loop that is mid-run.
        """
        self._state_ctl.set_active(value)

    def get_bg_pool(self) -> BackgroundTaskService:
        """Return the background task pool (capability surface; delegates)."""
        return self._capabilities.get_bg_pool()

    def get_sandbox_runtime(self) -> Optional[SandboxRuntime]:
        """Return the OS-level sandbox runtime, or ``None`` when not configured.

        Capability surface for the command-execution tools (Bash / terminal /
        python). ``None`` when ``permissions.runtime`` is absent/disabled, in
        which case those tools run un-sandboxed (the historical behavior).
        """
        return self._components.sandbox_runtime

    def get_device_config(self) -> DeviceConfig:
        """Return the DeviceUse tool's device-backend config (``config.tools.device``).

        Capability surface for the DeviceUse tool: selects the device backend
        (``auto``/``android``/``none``) and how to reach an adb device
        (``adb_path`` + ``serial``), without the executor layer reaching into the
        config. Defaults to ``auto`` (Android when adb is reachable, else null).
        """
        return self.config.tools.device

    def get_browser_stealth(self) -> bool:
        """Return the role's ``browser_stealth`` flag (True => anti-detection).

        Capability surface for the WebBrowser tool: lets the tool apply the
        opt-in stealth measures (realistic UA, ``navigator.webdriver`` hiding,
        launch flags) when the role opts in, without the executor layer reaching
        into the role schema. Defaults to False (no anti-detection).
        """
        return self.role_schema.browser_stealth

    def get_browser_locale(self) -> str:
        """Return the browser locale/region bundle key ("auto"/"en"/"zh").

        Capability surface for the WebBrowser tool: selects which coherent locale
        bundle the stealth fingerprint uses (only when ``browser_stealth`` is on).
        Resolution: an explicit per-role ``role_schema.browser_locale`` (anything
        other than "auto") wins, else it falls back to the global
        ``config.tools.browser_locale`` from config.yaml. When both are "auto"
        the engine infers zh vs en from the host env.
        """
        if self.role_schema.browser_locale != "auto":
            return self.role_schema.browser_locale
        configured = self.config.tools.browser_locale or ""
        if configured:
            return configured
        return "auto"

    def get_browser_proxy(self) -> str:
        """Return the browser's proxy URL (empty = direct connection).

        Capability surface for the WebBrowser tool: a single proxy URL giving the
        session one exit IP (parsed engine-side into Playwright's launch proxy
        dict). Resolution order (first non-empty wins):
          1. per-role ``role_schema.browser_proxy``;
          2. global ``config.tools.proxy`` from config.yaml (documented there as
             the proxy "for tools such as browsers");
          3. the ambient proxy env vars (``HTTPS_PROXY`` / ``HTTP_PROXY`` /
             ``ALL_PROXY``, case-insensitive) — so a shell that already exports a
             proxy routes the browser through it with no config. Playwright's
             Chromium does not read these itself, so we forward them explicitly.
        Defaults to "".
        """
        if self.role_schema.browser_proxy:
            return self.role_schema.browser_proxy
        configured = self.config.tools.proxy or ""
        if configured:
            return configured
        for var in (
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            value = os.environ.get(var, "")
            if value:
                return value
        return ""

    def get_browser_cdp_endpoint(self) -> str:
        """Return the optional endpoint for a user-owned Chrome instance."""
        return self.role_schema.browser_cdp_endpoint

    def get_browser_profile(self) -> str:
        """Return the role's durable browser-login profile name (empty = ephemeral).

        Capability surface for the WebBrowser tool: when non-empty, the tool
        seeds/persists the logged-in session from an encrypted profile under
        ``~/.mote/browser_profiles/`` instead of leaving login ephemeral.
        """
        return self.role_schema.browser_profile

    def load_browser_profile(self, name: str) -> Optional[dict]:
        """Return the decrypted ``storage_state`` saved under *name* (or None).

        Capability surface for the WebBrowser tool; delegates to the encrypted
        :class:`BrowserProfileStore`. Best-effort (None on any miss/failure).
        """
        return self.browser_profile_store.load(name)

    def save_browser_profile(self, name: str, storage_state: Optional[dict]) -> None:
        """Persist *storage_state* under *name* in the encrypted profile store.

        Capability surface for the WebBrowser tool; delegates to the encrypted
        :class:`BrowserProfileStore`. Best-effort (never raises).
        """
        self.browser_profile_store.save(name, storage_state)

    def get_browser_client_certs(self) -> list[dict]:
        """Return the role's client TLS certs as Playwright-shaped dicts.

        Capability surface for the WebBrowser tool (mutual-TLS logins). Maps each
        ``role_schema.browser_client_certs`` entry to a Playwright
        ``client_certificates`` dict: ``origin`` plus whichever of ``certPath`` /
        ``keyPath`` / ``pfxPath`` / ``passphrase`` are set (omitting empties). The
        ``passphrase`` is returned VERBATIM — it may be a secret placeholder that
        the tool expands from the vault at launch time — so no plaintext leaves
        the vault here. Empty list (default) means no mTLS.
        """
        out: list[dict] = []
        for cert in self.role_schema.browser_client_certs:
            entry: dict = {"origin": cert.origin}
            if cert.cert_path:
                entry["certPath"] = cert.cert_path
            if cert.key_path:
                entry["keyPath"] = cert.key_path
            if cert.pfx_path:
                entry["pfxPath"] = cert.pfx_path
            if cert.passphrase:
                entry["passphrase"] = cert.passphrase
            out.append(entry)
        return out

    def get_secret(self, key: str) -> Optional[str]:
        """Resolve a named secret to its plaintext value, or ``None`` if unknown.

        Capability surface for autonomous login-fill (WebBrowser): a tool
        references a credential **by key** (e.g. a ``<agent-vault:KEY>`` /
        ``<totp:KEY>`` placeholder the model wrote) and this resolves it against
        the encrypted vault — the model never authors or sees the value. Returns
        ``None`` when the key is unknown or secrets are disabled (no store),
        which the reference expander treats as fail-closed.
        """
        store = self._components.secret_store
        return store.get(key) if store is not None else None

    def list_secret_names(self) -> dict[str, str]:
        """Return the ``{name: placeholder}`` map of configured secret NAMES.

        The discovery complement of :meth:`get_secret`: the credential-index
        turn-context source reads this to show the model WHICH named secrets it
        can reference (and the exact placeholder to write), never any value.
        Reads the same vault; returns ``{}`` when secrets are disabled (no
        store). Wired into the context source by a lambda in ``role_components``
        (like the deferred-tool menu) — not published as a tool capability,
        because no tool consumes it.
        """
        store = self._components.secret_store
        return store.labels() if store is not None else {}

    async def ask_user(self, question: str) -> str:
        """Ask the user a question and return their response.

        Only valid inside a MoteEnv. A trailing 'stop' deactivates the role.
        Capability surface; delegates to :class:`RoleCapabilities`.
        """
        return await self._capabilities.ask_user(question)

    async def ask_user_question(self, questions: list[AskUserQuestionItem]) -> AskUserQuestionAnswers:
        """Ask the user structured multiple-choice questions; return structured answers.

        Capability surface behind the ``AskUserQuestion`` tool; delegates to
        :class:`RoleCapabilities`.
        """
        return await self._capabilities.ask_user_question(questions)

    async def handoff_runtime(self, runtime: str, *, message: str = ""):
        """Transfer a managed Runtime to the user through the active host surface."""
        return await self._capabilities.handoff_runtime(runtime, message=message)

    async def request_approval(self, request: "ApprovalRequest") -> "ApprovalChoice":
        """Ask the human to approve a gated tool call; return their decision.

        The interactive channel for the PermissionEngine's ``ask`` decisions.
        Capability surface; delegates to :class:`RoleCapabilities`.
        """
        return await self._capabilities.request_approval(request)

    async def reply_to_user(self, content: str) -> str:
        """Reply to the user with the provided content.

        Only valid inside a MoteEnv. Capability surface; delegates to
        :class:`RoleCapabilities`.
        """
        return await self._capabilities.reply_to_user(content)

    async def wait_interruptible(self, duration: "float | None" = None) -> float:
        """Block until an event wakes the agent, optionally bounded by *duration*.

        Capability surface for the Sleep tool; delegates to
        :class:`RoleCapabilities` (which owns the wait coordination). A positive
        *duration* opens a durable timer whose deadline survives a crash-resume.
        """
        return await self._capabilities.wait_interruptible(duration)

    async def dispatch_tool(self, name: str, kwargs: "dict | None" = None) -> "ToolResult":
        """Dispatch a nested tool call through the executor chokepoint.

        Capability surface for the ``run_graph`` orchestrator: every graph-node
        tool call is routed back through ``ToolExecutor.run_command``, so the same
        permission gate, hooks, and observability that guard a direct tool call
        apply identically to graph-driven calls (re-entrant safe). Returns the
        tool's ``ToolResult`` — a denied/failed call is ``success=False``, not raised.

        Deliberately passes NO ``result_id``, so graph-internal calls are not
        individually ledgered by the effect-ledger. Crash recovery for a graph is
        two-level: (1) the foreground ``run_graph`` runs inside ONE top-level
        ``run_command`` whose own EXTERNAL ledger entry (RunGraph resolves to
        EXTERNAL) is the crash-recovery unit — a resume reconciles that single
        call; (2) in-flight pause/resume re-runs only not-yet-completed NODES.
        Ledgering each node call would be unreconcilable: a graph-internal call
        never surfaces as a ``tool_calls`` entry in the durable history, so the
        resume reconciler could never pair (and reap) it — its ``started`` record
        would leak forever. So node-level idempotency stays a graph concern
        (node-replay), and the ledger guards the graph as a whole.
        """
        return await self.executor.run_command(name, kwargs or {})

    async def commit_graph_output(
        self,
        *,
        output: Any,
        contract_spec: Any,
        run_id: str,
    ) -> Any:
        """Validate and durably commit one graph output through the lazy service."""

        return await self._components.graph_output_service.finalize(
            output=output,
            contract_spec=contract_spec,
            run_id=run_id,
        )

    async def resume_graph_output(self, *, contract_spec: Any, run_id: str) -> Any:
        """Resume one graph output through the lazy durable output service."""

        return await self._components.graph_output_service.resume(
            contract_spec=contract_spec,
            run_id=run_id,
        )

    @asynccontextmanager
    async def graph_run_lease(self, run_id: str):
        """Own a graph from recovery lookup through its terminal commit."""
        await self._components.begin_graph_lease(run_id)
        try:
            yield
        finally:
            await self._components.end_graph_lease(run_id)

    def list_tool_names(self) -> list[str]:
        """Live tool names (primary + aliases), for ``run_graph`` to validate the
        tool references in a graph spec. Capability surface over the executor."""
        return self.executor.tool_names()

    def list_graph_tool_names(self) -> list[str]:
        """Names of tools that are themselves graph orchestrators, for ``run_graph``
        to refuse nesting a graph inside a graph. Capability surface over the
        executor's ``is_graph_tool`` marker set."""
        return sorted(self.executor.graph_tool_names())

    def list_graph_excluded_tool_names(self) -> list[str]:
        """Names of tools that must not appear as a node inside a graph, for
        ``run_graph`` to refuse referencing them (e.g. Sleep, which blocks on an
        external wake event a foreground graph never delivers). Capability surface
        over the executor's ``graph_excluded`` marker set."""
        return sorted(self.executor.graph_excluded_tool_names())

    def list_deferred_tools(self) -> dict[str, str]:
        """The deferred-tool MATCH corpus → ``{name: summary + recall keywords}``.

        Capability surface for the ``SearchTools`` meta-tool: the full set of
        deferred (not-yet-revealed) tools it searches over, each entry enriched
        with the tool's recall keywords so a synonym the one-line summary omits
        still resolves. This is the SEARCH layer — distinct from the DISPLAY menu
        the model reads (``deferred_tool_index``, pure summaries). Delegates to
        the executor's tool catalog (the single deferral seam)."""
        return self.executor.deferred_search_index()

    def reveal_tools(self, names: list[str]) -> list[str]:
        """Reveal deferred tools by name so their full schema is sent next turn.

        Capability surface for the ``SearchTools`` meta-tool. Intersects *names*
        with the executor's deferred set (so only real deferred tool names are
        accepted — a bogus/non-deferred name is ignored), records the accepted
        names on ``RoleState`` (durable across resume) and returns them. The next
        ``prepare()`` includes their schema on the active channel."""
        deferred = set(self.executor.deferred_tool_index().keys())
        accepted = [n for n in names if n in deferred]
        self._state_ctl.reveal_tools(accepted)
        return accepted

    def describe_deferred_tools(self, names: list[str]) -> dict[str, str]:
        """Full descriptions of the named deferred tools → ``{name: description}``.

        Capability surface for the ``SearchTools`` meta-tool: on reveal it reads
        the newly-revealed tools' full prose (stripped off the split ``tools=``
        wire) so it can persist that description into the conversation +
        ResourceRegistry. Delegates to the executor's tool catalog."""
        return self.executor.describe_deferred_tools(names)

    async def describe_image(self, artifact: "ArtifactRef", *, prompt: str = "") -> str:
        """Read an image as text via an ISOLATED vision-model call.

        Capability surface for ``WebBrowser``'s ``read_image`` action. Routes a
        secondary LLM call via the ``image_description`` task (a multimodal
        small/fast model — see ``ModelsConfig``) so the caller never touches the
        router/Context directly, mirroring every other capability. It reads an
        on-page image as text: normally an image reaches the model directly as
        media (Read → ToolMedia), but the browser has no such wire for an
        in-page ``<img>`` — this routes it to a vision model instead, whose
        textual reading then re-enters the conversation as ordinary text.

        Raises:
            NotImplementedError: The routed model is not vision-capable — the
                tool degrades to an "image understanding unavailable" notice.
        """
        return await describe_image_with_model(
            self.router.model_route_for_task("image_description"),
            artifact,
            model_call_id=uuid4().hex,
            prompt=prompt,
        )

    async def end_session(self) -> str:
        """End the current session and produce a summary if configured.

        Capability surface for the End tool; delegates to
        :class:`RoleCapabilities`.
        """
        return await self._capabilities.end_session()

    def get_memories(self, k=0) -> list[Message]:
        return self.context_manager.get(k=k)

    def publish_message(self, msg):
        """If the role belongs to env, then the role's messages will be broadcast to env"""
        if not msg:
            return
        if MESSAGE_ROUTE_TO_SELF in msg.send_to:
            msg.send_to.add(any_to_str(self))
            msg.send_to.remove(MESSAGE_ROUTE_TO_SELF)
        if not msg.sent_from or msg.sent_from == MESSAGE_ROUTE_TO_SELF:
            msg.sent_from = any_to_str(self)
        if all(to in {any_to_str(self), self.role_schema.name} for to in msg.send_to):
            self.put_message(msg)
            return
        if not self.state.env:
            return
        if isinstance(msg, AIMessage) and not msg.agent:
            msg.with_agent(self.role_schema.display_name)
        self.state.env.publish_message(msg)

    def put_message(self, message):
        """Place the message into the Role object's private message buffer."""
        self._state_ctl.put_message(message)

    def _coerce_to_message(self, with_message) -> Message:
        """Normalize the run() input (str / list / Message) into one Message.

        Stamps the default USER_REQUIREMENT cause and routes the message to this
        role so the flow observes it. Kept out of run() so the dispatch table
        stays readable.
        """
        if isinstance(with_message, Message):
            msg = with_message
        elif isinstance(with_message, list):
            msg = Message(content="\n".join(with_message))
        else:
            msg = Message(content=with_message)
        if not msg.cause_by:
            msg.cause_by = CauseBy.USER_REQUIREMENT
        msg.send_to.add(self.role_schema.name)
        return msg

    async def _emit_session_start(self) -> None:
        """Emit ``SessionStartEvent`` exactly once across this Role's run() calls.

        The HookSubscriber fires the SessionStart hook; the recorder's meta line
        is written here at the explicit startup boundary. Also starts the opt-in
        external-file watcher (the property is None when disabled / no hook
        layer); its polling loop is stopped in :meth:`cleanup`.
        """
        if self._session_started:
            return
        self._session_started = True

        # Session metadata is an ordinary first fact, committed before any
        # observer can append later session facts.
        if not self.session_log.exists():
            await self.session_log.append(
                SessionMetaEvent(
                    session_id=self.state.session_id,
                    parent_session_id=self.state.parent_session_id,
                    working_dir=self.state.working_dir,
                    original_working_dir=self.state.original_working_dir,
                    project_root=self.state.project_root,
                    model=getattr(self.config.models.default, "model", None),
                    role_class=f"{type(self).__module__}.{type(self).__qualname__}",
                    toolset_manifest=toolset_manifest(self.wiring.dependencies.toolsets),
                )
            )

        # Open this session's own log file (logs/{session_id}.txt), named to
        # match its workspace session folder. run() has bound session_id as the
        # trace_id, so the sink's filter routes this session's lines here.
        bind_session_logfile(self.session_id, CONFIG_ROOT / "logs")

        await self.telemetry.emit(
            SessionStartEvent(
                session_id=self.state.session_id,
                parent_session_id=self.state.parent_session_id,
                working_dir=self.state.working_dir,
                original_working_dir=self.state.original_working_dir,
                project_root=self.state.project_root,
                model=getattr(self.config.models.default, "model", None),
                role_class=f"{type(self).__module__}.{type(self).__qualname__}",
                source="startup",
            )
        )

        watcher = self.file_watch_service
        if watcher is not None and not watcher.watcher.is_running():
            await watcher.start_async()

        self._components.kickoff_artifact_gc()

        # Kick off the whole-repo code-index cold scan off the event loop (Layer
        # C). No-op when the index layer is off; best-effort inside.
        await self._components.kickoff_repo_scan()

        # Reclaim stale workspace artifacts off the event loop (throttled ~daily,
        # excludes this session). No-op when disabled; best-effort inside.
        await self._components.kickoff_workspace_cleanup()

    @role_raise_decorator
    async def run(self, with_message=None) -> RunOutcome[OutputT] | None:
        """Run one request and return its typed, durably committed output."""
        # Bind the session_id as the trace_id so every log line emitted during
        # this run (across the flow, think engine, executor, etc.) is correlated.
        # Bind Telemetry to the async context so deep call sites (the LLM
        # client streaming tokens, a tool capturing a snapshot) can emit onto the
        # same runtime without threading it through every signature.
        with (
            bind_trace(self.session_id),
            bind_telemetry(self.telemetry),
        ):
            async with span(f"role.run:{self.name}"):
                await self._ensure_ready()
                await self._emit_session_start()

                if with_message:
                    msg = self._coerce_to_message(with_message)

                    decision = await self.prompt_policy.process(PromptIntent(prompt=msg.content))
                    msg.content = decision.prompt
                    if not decision.accepted:
                        event = PromptRejectedEvent(
                            prompt=decision.prompt,
                            reason=decision.reason,
                            terminate=decision.terminate,
                        )
                        await self._components.session_fact_committer.commit_fact(event)
                        await self.telemetry.emit(event)
                        return RunRejected(
                            kind=RunRejectionKind.PROMPT_ADMISSION,
                            reason=decision.reason,
                            terminate=decision.terminate,
                            transcript=TranscriptRef(session_id=self.session_id),
                        )

                    await self.telemetry.emit(UserPromptSubmitEvent(prompt=decision.prompt))
                    if decision.additional_context:
                        injected = "\n".join(decision.additional_context)
                        msg.content = f"{injected}\n{msg.content}" if msg.content else injected

                    # LSP diagnostics now flow through the per-turn ephemeral-context
                    # bus (turn_context layer): drained every think() cycle into the
                    # user prompt's <system-reminder> (never stored in history),
                    # alongside git status / token pressure / background-task feeds.
                    self.put_message(msg)

                # Auto-continue is a bounded completion-policy decision. A turn
                # always ends as an immutable fact; no subscriber can veto it.
                auto_continue_budget = self.role_schema.max_auto_continue
                flow_result = None
                while True:
                    lease = await self._components.begin_output_lease()
                    try:
                        run_context = RunContext(
                            deps=self.deps,
                            session_id=self.session_id,
                            run_id=lease.run_id,
                        )
                        with bind_run_context(run_context):
                            await self.executor.start_run(run_context)
                            try:
                                flow_engine = self._components.make_flow_engine()
                                try:
                                    flow_result = await flow_engine.run()
                                finally:
                                    # Always propagate for recovery (role_raise_decorator reads it).
                                    self.state.latest_observed_msg = flow_engine.latest_observed_msg
                                    # TurnEnd is an immutable observation fact.
                                    await self._emit_turn_end()
                            finally:
                                await self.executor.end_run()
                    finally:
                        await self._components.end_output_lease()
                    bg_pool = self._peek_bg_pool()
                    completion = await self.run_completion_policy.process(
                        RunCompletionIntent(
                            output_committed=(flow_result is not None and flow_result.committed_output is not None),
                            background_pending=(bg_pool is not None and bg_pool.has_pending()),
                            remaining_continuations=auto_continue_budget,
                        )
                    )
                    if not self._apply_continuation_decision(completion):
                        break
                    auto_continue_budget -= 1
                if flow_result is None:
                    return None
                rsp = flow_result.presentation

                # Post-loop finalization (was Role.react): clear the active signal
                # and tag the response with this Role's display name.
                self._state_ctl.deactivate()
                if isinstance(rsp, AIMessage):
                    rsp.with_agent(self.role_schema.display_name)
                committed = flow_result.committed_output
                if committed is not None:
                    publication_id = f"output:{self.session_id}:{committed.run_id}"
                    queued_event = OutputPublicationQueuedEvent(
                        publication_id=publication_id,
                        candidate_id=committed.candidate_id,
                        contract_id=committed.contract_id,
                        run_id=committed.run_id,
                        run_kind=committed.run_kind.value,
                    )
                    await self._components.session_fact_committer.commit_fact(queued_event)
                    await self.telemetry.emit(queued_event)
                    await self.context.disk_writer.drain()
                    rsp.metadata["output_publication_id"] = publication_id
                    self.publish_message(rsp)
                    published_event = OutputPublishedEvent(
                        candidate_id=committed.candidate_id,
                        contract_id=committed.contract_id,
                        publication_id=publication_id,
                        run_id=committed.run_id,
                        run_kind=committed.run_kind.value,
                    )
                    await self._components.session_fact_committer.commit_fact(published_event)
                    await self.telemetry.emit(published_event)
                    await self.context.disk_writer.drain()
                    return RunResult(
                        output=committed.value,
                        output_record=committed,
                        transcript=TranscriptRef(
                            session_id=self.session_id,
                            terminal_message_id=str(rsp.id),
                        ),
                        run_id=committed.run_id,
                    )
                self.publish_message(rsp)
                return None

    @staticmethod
    def list_sessions(base_dir: str | None = None, *, cwd: str | None = None) -> list:
        """List resumable sessions (newest first); see ``session.list_sessions``.

        A thin, discoverable entry point onto the lite directory scan. ``cwd``
        filters to sessions started under that working dir / project root.
        """

        return _list(base_dir, cwd=cwd)

    def resume_session(self) -> bool:
        """Rebuild this role's stored history from its durable rollout log.

        Thin delegator onto :class:`RoleSessionManager` (which owns the replay +
        registry/restore re-seeding). Returns False when no log exists.
        """
        return self._session_manager.resume()

    def validate_resume_identity(self, meta: Mapping[str, object]) -> None:
        """Validate Role and Toolset identities at every recovery boundary."""

        self._session_manager.validate_identity(meta)

    def incarnation_blueprint(self) -> AgentIncarnationBlueprint:
        """Capture construction values for a sequential Residency replacement."""

        role_cls = type(self)
        role_type = f"{role_cls.__module__}.{role_cls.__qualname__}"
        config = self._config
        wiring = self.wiring

        def restore(snapshot: Mapping[str, Any]) -> BaseRole:
            schema = RoleSchema.model_validate(snapshot.get("role_schema", {}))
            state = RoleState.model_validate(snapshot.get("state", {}))
            return role_cls(
                role_schema=schema,
                state=state,
                config=config,
                wiring=wiring,
            )

        return AgentIncarnationBlueprint(
            role_type=role_type,
            snapshot_type_id=self.role_type_id,
            restore=restore,
        )

    async def fork_session(self) -> "Role":
        """Branch a sibling role off this session at its current history.

        Thin delegator onto :class:`RoleSessionManager`; returns a fresh role of
        the same class resumed onto the inherited history, independent afterwards.
        """
        return await self._session_manager.fork()

    def _apply_continuation_decision(self, decision: RunCompletionDecision) -> bool:
        """Enqueue policy-provided context when another bounded turn is required."""
        if not decision.continue_run:
            return False
        injected = "\n".join(decision.additional_context)
        if injected:
            self.put_message(self._coerce_to_message(injected))
        return True

    async def _emit_turn_end(self) -> None:
        """Commit and observe the immutable ``TurnEndEvent`` for one turn."""
        telemetry = self._components.peek_telemetry()
        try:
            token_state = None
            try:
                token_state = asdict(self.context_manager.token_state())
            except Exception:  # noqa: BLE001 — token math is optional metadata
                token_state = None
            event = TurnEndEvent(
                turn_id=uuid4().hex,
                working_dir=self.state.working_dir,
                model=getattr(self.config.models.default, "model", None),
                token_state=token_state,
            )
            await self._components.session_fact_committer.commit_fact(event)
            if telemetry is not None:
                await telemetry.emit(event)
        finally:
            try:
                await self._components.release_ephemeral_artifacts()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"session: failed to release ephemeral Artifacts: {exc}")

    async def cleanup(self) -> None:
        """Tear down session-scoped subsystems (best-effort, idempotent).

        Stops the file-watch polling loop (and detaches it from Telemetry),
        cancels the owned title-generation task, shuts the LSP language servers
        down, then delegates to
        :meth:`ToolExecutor.cleanup` (which closes the terminal/kernel). Safe to
        call when those subsystems were never built — each guard short-circuits.
        """
        if self._cleanup_complete:
            return

        task = self._cleanup_task
        if task is None or task.cancelled() or (task.done() and task.exception() is not None):
            task = asyncio.create_task(
                self._cleanup(release_services=True),
                name=f"mote-role-close-{self.session_id[:8]}",
            )
            self._cleanup_task = task
        await asyncio.shield(task)

    async def prepare_for_eviction(self) -> None:
        """Close this incarnation while transferring its services lease.

        Residency replacement is sequential, not a fork: the blueprint hands
        the same immutable wiring to the replacement after this Role is removed.
        All incarnation-owned resources close here, but the shared services
        ownership token must remain live for that transfer.
        """

        if self._cleanup_complete:
            return
        await self._cleanup(release_services=False)

    async def _cleanup(self, *, release_services: bool) -> None:
        """Run one complete teardown attempt behind :meth:`cleanup`'s shared task."""
        self._prepare_cleanup_lifecycle(release_services=release_services)
        await self._cleanup_lifecycle.aclose()
        self._cleanup_complete = True

    def _prepare_cleanup_lifecycle(self, *, release_services: bool) -> None:
        if self._cleanup_lifecycle_prepared:
            return
        self._cleanup_lifecycle_prepared = True
        lifecycle = self._cleanup_lifecycle

        lifecycle.register_close(
            "session-log",
            lambda: unbind_session_logfile(self.session_id),
            phase=LifecyclePhase.CLOSE_RESOURCES,
        )
        repo_index = self._components.peek_repo_index()
        if repo_index is not None:
            lifecycle.register_close(
                "repo-index",
                repo_index.close,
                phase=LifecyclePhase.CLOSE_RESOURCES,
            )
        lifecycle.register_close(
            "maintenance",
            self._components.close_maintenance,
            phase=LifecyclePhase.CLOSE_RESOURCES,
        )
        sandbox_runtime = self._components.peek_sandbox_runtime()
        if sandbox_runtime is not None:
            lifecycle.register_close(
                "sandbox",
                sandbox_runtime.shutdown,
                phase=LifecyclePhase.CLOSE_RESOURCES,
            )
        runtime_host = self._components.peek_runtime_host()
        if runtime_host is not None:
            lifecycle.register_close(
                "managed-runtimes",
                lambda: self._close_runtime_host(runtime_host),
                phase=LifecyclePhase.CLOSE_RESOURCES,
            )
        executor = self._components.peek_executor()
        if executor is not None:
            lifecycle.register_close(
                "tool-executor",
                executor.cleanup,
                phase=LifecyclePhase.CLOSE_RESOURCES,
            )
        telemetry = self._components.peek_telemetry()
        if telemetry is not None and self._components.telemetry_wired:
            lifecycle.register_close(
                "telemetry",
                telemetry.aclose,
                phase=LifecyclePhase.CLOSE_RESOURCES,
            )
        else:
            lsp_service = self._components.peek_lsp_service()
            if lsp_service is not None:
                lifecycle.register_close(
                    "lsp",
                    lsp_service.aclose,
                    phase=LifecyclePhase.CLOSE_RESOURCES,
                )
            title_subscriber = self._components.peek_title_subscriber()
            if title_subscriber is not None:
                lifecycle.register_close(
                    "title-subscriber",
                    title_subscriber.aclose,
                    phase=LifecyclePhase.CLOSE_RESOURCES,
                )
        event_fabric = self._components.peek_event_fabric()
        if event_fabric is not None:
            lifecycle.register_close(
                "event-fabric",
                event_fabric.aclose,
                phase=LifecyclePhase.FLUSH_DURABILITY,
            )
        bg_pool = self._components.peek_bg_pool()
        if bg_pool is not None:
            lifecycle.register_close(
                "background-tasks",
                bg_pool.aclose,
                phase=LifecyclePhase.CLOSE_RESOURCES,
            )
        file_watch_service = self._components.peek_file_watch_service()
        if file_watch_service is not None:
            lifecycle.register_close(
                "file-watch",
                file_watch_service.stop,
                phase=LifecyclePhase.CLOSE_RESOURCES,
            )
        if release_services and self.wiring.services_lease is not None:
            lifecycle.register_close(
                "engine-services-lease",
                self.wiring.services_lease.aclose,
                phase=LifecyclePhase.RELEASE_CONTAINER,
            )

    @staticmethod
    async def _close_runtime_host(runtime_host) -> None:
        failures = await runtime_host.close_all()
        if failures:
            details = "; ".join(f"{runtime}: {type(exc).__name__}: {exc}" for runtime, exc in failures.items())
            raise RuntimeError(f"managed runtime shutdown failed: {details}")

    # =========================================================================
    # Readiness
    # =========================================================================

    async def _ensure_ready(self):
        """Lazy init for expensive/fallible subsystems."""
        await self._components.start_event_fabric()
        # Materialize the ContextManager (stored-history store + compaction
        # orchestrator), backed by RoleState.context so it survives recovery.
        _ = self.context_manager

        # Wire telemetry by subscribing the fixed roster. The ``telemetry`` getter is
        # a pure leaf — it never wires itself — so this explicit step is the sole
        # trigger, guaranteeing handlers are wired before the first ``emit`` below
        # (``bind_telemetry`` in run() bound the same leaf, mutated in place here).
        await self._components._wire_telemetry()

        # Wire runtime edges between built collaborators (the router's COMPRESS
        # reducer ← ContextManager). Split out of the getters so no component
        # read mutates a sibling as a hidden side-effect.
        self._components._wire_collaborators()

        await self._components.reconcile_artifact_publications_once()
        await self._components.reconcile_runtime_projections_once()

        self.skill_manager.ensure_ready()
        await self.executor.init_mcp(self.role_schema.mcps, enabled=self.config.mcp.enabled)
