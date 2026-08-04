"""Tool execution, command protocol, and background-action manifest."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar
from uuid import uuid4

from mote.contracts.ports.task.operations import (
    BackgroundTaskBuildContext,
    BackgroundTaskService,
    BackgroundTaskServiceFactory,
)
from mote.contracts.ports.tool.deferred import DeferredResultProjectorFactory
from mote.contracts.ports.tool.policy import ToolCallPolicyExtensionSpec
from mote.contracts.session.identity import SessionId
from mote.contracts.task.lifecycle import BackgroundTaskOwner
from mote.contracts.workflow.execution import WorkflowNodeDispatchResult
from mote.kernel.commands import make_command_channel
from mote.runtime.agent.component_graph import BuildContext, ComponentSpec
from mote.runtime.agent.component_keys import (
    ARTIFACT_PUBLISHER,
    ARTIFACT_RESOLVER,
    BACKGROUND_POOL,
    BROWSER_PROFILE_STORE,
    COMMAND_CHANNEL,
    EXECUTOR,
    GRAPH_OUTPUT_SERVICE,
    HOOK_MANAGER,
    PERMISSION_ENGINE,
    SECRET_STORE,
    SESSION_FACT_COMMITTER,
    SKILL_MANAGER,
    TELEMETRY,
    TOOL_CALL_POLICY,
    TOOL_RESULT_POLICY,
    WORKSPACE_STORE,
)
from mote.runtime.config.mcp import MCPServerConfig
from mote.runtime.interactive.browser.profile import BrowserProfileStore
from mote.runtime.models.media_projection import build_media_materializer
from mote.runtime.output.graph_service import GraphOutputService
from mote.runtime.secrets.cipher import build_aes_cipher
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.tools.policy import build_permission_engine, build_tool_call_policy, build_tool_result_policy
from mote.runtime.tools.provider import ContextFreeToolset
from mote.runtime.tools.tool_executor import ToolExecutor

AgentDepsT = TypeVar("AgentDepsT")
_PROCESS_INSTANCE_ID = uuid4().hex


async def _noop_async() -> None:
    return None


RoleBuildContext = BuildContext[Any, Any]


@dataclass(frozen=True, slots=True)
class ActionComponentInputs:
    session_workspace_root: Path | None = None
    secrets_root: Path | None = None
    browser_profiles_root: Path | None = None
    oauth_root: Path | None = None
    toolsets: tuple[ContextFreeToolset, ...] = ()
    tool_policy_extensions: tuple[ToolCallPolicyExtensionSpec, ...] = ()
    deferred_result_projector_factory: DeferredResultProjectorFactory | None = None
    mcp_servers: tuple[MCPServerConfig, ...] = ()
    background_task_pool_builder: BackgroundTaskServiceFactory | None = None


def _missing_background_pool_builder(
    _ctx: BackgroundTaskBuildContext,
) -> BackgroundTaskService:
    raise RuntimeError(
        "background task orchestration is not installed; inject "
        "background_task_pool_builder at the Product composition root"
    )


def _build_workspace_store(ctx: RoleBuildContext, inputs: ActionComponentInputs) -> SessionWorkspace:
    root = inputs.session_workspace_root
    if root is None:
        raise ValueError("Agent composition requires a session workspace root")
    return SessionWorkspace(root)


@dataclass(frozen=True, slots=True)
class _AgentWake:
    callback: Callable[[], None] | None

    def wake(self) -> None:
        if self.callback is None:
            raise RuntimeError("Agent wake callback is not bound")
        self.callback()


@dataclass(frozen=True, slots=True)
class _RoleWorkflowNodeExecution:
    role: RoleBuildContext

    async def dispatch(self, tool_name: str, arguments: dict[str, object]) -> WorkflowNodeDispatchResult:
        result = await self.role.role.dispatch_tool(tool_name, dict(arguments))
        return WorkflowNodeDispatchResult(result.output, result.success, result.payload, result.error)

    def allowed_tool_names(self) -> tuple[str, ...]:
        role = self.role.role
        return tuple(
            sorted(
                set(role.list_tool_names())
                - set(role.list_graph_tool_names())
                - set(role.list_graph_excluded_tool_names())
            )
        )


def _build_background_pool(ctx: RoleBuildContext, builder: BackgroundTaskServiceFactory) -> BackgroundTaskService:
    role = ctx.role
    wake = ctx.state.pending_task_completion_wake
    return builder(
        BackgroundTaskBuildContext(
            message_sink=role.state.msg_buffer,
            wake=_AgentWake(wake),
            output_locations=ctx.dep(WORKSPACE_STORE),
            session_id=SessionId(role.state.session_id),
            result_registry=role._capabilities,
            owner=BackgroundTaskOwner(
                process_instance_id=_PROCESS_INSTANCE_ID,
                agent_id=role.state.session_id,
                incarnation_id=role.incarnation_id,
            ),
        )
    )


def action_component_specs(
    background_pool_builder: BackgroundTaskServiceFactory | None = None,
    *,
    inputs: ActionComponentInputs = ActionComponentInputs(),
) -> list[ComponentSpec[Any, Any, object]]:
    background_pool_builder = (
        inputs.background_task_pool_builder or background_pool_builder or _missing_background_pool_builder
    )
    return [
        ComponentSpec(
            WORKSPACE_STORE,
            lambda ctx: _build_workspace_store(ctx, inputs),
        ),
        ComponentSpec(
            BACKGROUND_POOL,
            lambda ctx: _build_background_pool(ctx, background_pool_builder),
        ),
        ComponentSpec(PERMISSION_ENGINE, lambda ctx: _build_permission_engine(ctx, inputs)),
        ComponentSpec(TOOL_CALL_POLICY, lambda ctx: _build_tool_call_policy(ctx, inputs)),
        ComponentSpec(TOOL_RESULT_POLICY, _build_tool_result_policy),
        ComponentSpec(EXECUTOR, lambda ctx: _build_executor(ctx, inputs)),
        ComponentSpec(COMMAND_CHANNEL, _build_command_channel),
        ComponentSpec(GRAPH_OUTPUT_SERVICE, _build_graph_output_service),
        ComponentSpec(BROWSER_PROFILE_STORE, lambda ctx: _build_browser_profile_store(ctx, inputs)),
    ]


def effective_deferred_tools(role: Any) -> set[str]:
    """Return the active deferred-tool set after the global engagement gate."""
    if not role.config.tools.tool_search.enabled:
        return set()
    return set(role.role_schema.deferred_tools)


def dedupe_tools(tools: list[str]) -> list[str]:
    """Remove duplicate tool names while preserving first-seen order."""
    seen: set[str] = set()
    deduped: list[str] = []
    for tool in tools:
        if tool not in seen:
            seen.add(tool)
            deduped.append(tool)
    return deduped


def build_args_limiter(executor: ToolExecutor[object]) -> Callable[[str, Any, str | None], Any]:
    """Adapt the executor's lossless large-argument persistence seam."""

    def limit(_tool_name: str, args: Any, call_id: str | None) -> Any:
        return executor.persist_large_args(args, call_id)

    return limit


def _build_command_channel(ctx: RoleBuildContext):
    return make_command_channel(
        ctx.role.role_schema.command_protocol,
        args_limiter=build_args_limiter(ctx.dep(EXECUTOR)),
        output_is_text=ctx.role.output_contract.is_text,
        media_materializer=build_media_materializer(ctx.dep(ARTIFACT_RESOLVER)),
    )


def _build_browser_profile_store(ctx: RoleBuildContext, inputs: ActionComponentInputs) -> BrowserProfileStore:
    secrets_root = inputs.secrets_root
    profiles_root = inputs.browser_profiles_root
    if secrets_root is None or profiles_root is None:
        raise ValueError("Agent composition requires secrets and browser profile roots")
    return BrowserProfileStore(
        lambda: build_aes_cipher(secrets_root / "vault.key"),
        root=profiles_root,
    )


def _build_graph_output_service(ctx: RoleBuildContext) -> GraphOutputService:
    services = ctx.role._wiring.services
    context = services.context if services is not None else None
    return GraphOutputService(
        take_restore=ctx.role._state_ctl.take_pending_graph_output_restore,
        current_lease=ctx.role._components.current_graph_lease,
        drain_writes=context.disk_writer.drain if context is not None else _noop_async,
        session_fact_sink=ctx.dep(SESSION_FACT_COMMITTER),
    )


def _build_tool_call_policy(ctx: RoleBuildContext, inputs: ActionComponentInputs):
    role = ctx.role
    toolsets = inputs.toolsets
    return build_tool_call_policy(
        role.role_schema.permissions,
        role=role,
        hook_manager=ctx.dep(HOOK_MANAGER),
        extensions=inputs.tool_policy_extensions,
        require_permission=any(toolset.requires_permission_gate for toolset in toolsets),
        permission_engine=ctx.dep(PERMISSION_ENGINE),
    )


def _build_permission_engine(ctx: RoleBuildContext, inputs: ActionComponentInputs):
    role = ctx.role
    return build_permission_engine(
        role.role_schema.permissions,
        role=role,
        require_permission=any(toolset.requires_permission_gate for toolset in inputs.toolsets),
    )


def _build_tool_result_policy(ctx: RoleBuildContext):
    return build_tool_result_policy(
        hook_manager=ctx.dep(HOOK_MANAGER),
        secret_store=ctx.dep(SECRET_STORE),
        loop_guard_config=ctx.role.config.tools.loop_guard,
    )


def _build_executor(ctx: RoleBuildContext, inputs: ActionComponentInputs) -> ToolExecutor[object]:
    role = ctx.role
    all_tools = role.role_schema.mcps + role.role_schema.tools
    if ctx.dep(SKILL_MANAGER).enabled:
        all_tools = [*all_tools, "Skill"]
    deferred_tools = effective_deferred_tools(role)
    if deferred_tools:
        all_tools = [*all_tools, "SearchTools"]
    tools_cfg = role.config.tools
    projector_factory = inputs.deferred_result_projector_factory
    deferred_projector = (
        projector_factory(
            ctx.dep(BACKGROUND_POOL),
            ctx.dep(ARTIFACT_PUBLISHER),
            _RoleWorkflowNodeExecution(ctx),
        )
        if projector_factory is not None
        else None
    )
    return ToolExecutor(
        session_id=role.state.session_id,
        tools=dedupe_tools(all_tools),
        role=role,
        tool_call_policy=ctx.dep(TOOL_CALL_POLICY),
        tool_result_policy=ctx.dep(TOOL_RESULT_POLICY),
        limit_config=tools_cfg.result_limit,
        effect_store_config=tools_cfg.effect_store,
        loop_guard_config=tools_cfg.loop_guard,
        telemetry=ctx.dep(TELEMETRY),
        deferred_result_projector=deferred_projector,
        pipelines_enabled=role.config.context.bggraph.enabled,
        workspace_store=ctx.dep(WORKSPACE_STORE),
        deferred_tools=deferred_tools,
        get_revealed=lambda: role.state.revealed_tools,
        toolsets=inputs.toolsets,
        command_protocol=role.role_schema.command_protocol,
        mcp_servers=list(inputs.mcp_servers),
        oauth_root=inputs.oauth_root,
    )


__all__ = [
    "action_component_specs",
    "ActionComponentInputs",
    "build_args_limiter",
    "dedupe_tools",
    "effective_deferred_tools",
]
