"""Tool execution, command protocol, and background-action manifest."""
from __future__ import annotations

from typing import Any

from mote.kernel.parser import make_command_channel
from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.agent.graph_output_service import GraphOutputService
from mote.runtime.secrets.cipher import build_cipher
from mote.runtime.tools.dependency.browser_profile import BrowserProfileStore
from mote.runtime.tools.policy import build_tool_call_policy, build_tool_result_policy
from mote.runtime.tools.tool_executor import ToolExecutor
from mote.runtime.workspace import WorkspaceStore


async def _noop_async() -> None:
    return None


def _missing_background_pool_builder(_ctx):
    raise RuntimeError(
        "background task orchestration is not installed; inject "
        "background_task_pool_builder at the Product composition root"
    )


def action_component_specs(background_pool_builder=None) -> list[ComponentSpec]:
    background_pool_builder = background_pool_builder or _missing_background_pool_builder
    return [
        ComponentSpec("workspace_store", lambda ctx: WorkspaceStore()),
        ComponentSpec("bg_pool", background_pool_builder),
        ComponentSpec("tool_call_policy", _build_tool_call_policy),
        ComponentSpec("tool_result_policy", _build_tool_result_policy),
        ComponentSpec("executor", _build_executor),
        ComponentSpec("command_channel", _build_command_channel),
        ComponentSpec("graph_output_service", _build_graph_output_service),
        ComponentSpec("browser_profile_store", _build_browser_profile_store),
    ]


def effective_deferred_tools(role) -> set[str]:
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


def build_args_limiter(executor):
    """Adapt the executor's lossless large-argument persistence seam."""

    def limit(_tool_name: str, args: Any, call_id: str | None) -> Any:
        return executor.persist_large_args(args, call_id)

    return limit


def _build_command_channel(ctx):
    return make_command_channel(
        ctx.role.role_schema.command_protocol,
        args_limiter=build_args_limiter(ctx.dep("executor")),
        output_is_text=ctx.role.output_contract.is_text,
        artifact_resolver=ctx.dep("artifact_resolver"),
    )


def _build_browser_profile_store(ctx):
    secrets_cfg = ctx.role.config.secrets
    return BrowserProfileStore(lambda: build_cipher(secrets_cfg))


def _build_graph_output_service(ctx) -> GraphOutputService:
    services = ctx.role.wiring.services
    context = services.context if services is not None else None
    return GraphOutputService(
        take_restore=ctx.role._state_ctl.take_pending_graph_output_restore,
        current_lease=ctx.role._components.current_graph_lease,
        drain_writes=context.disk_writer.drain if context is not None else _noop_async,
        session_fact_sink=ctx.dep("session_fact_committer"),
    )


def _build_tool_call_policy(ctx):
    role = ctx.role
    toolsets = role.wiring.dependencies.toolsets
    return build_tool_call_policy(
        role.role_schema.permissions,
        role=role,
        hook_manager=ctx.dep("hook_manager"),
        extensions=role.wiring.dependencies.tool_policy_extensions,
        require_permission=any(toolset.requires_permission_gate for toolset in toolsets),
    )


def _build_tool_result_policy(ctx):
    return build_tool_result_policy(
        hook_manager=ctx.dep("hook_manager"),
        secret_store=ctx.dep("secret_store"),
        loop_guard_config=ctx.role.config.tools.loop_guard,
    )


def _build_executor(ctx) -> ToolExecutor:
    role = ctx.role
    all_tools = role.role_schema.mcps + role.role_schema.tools
    if ctx.dep("skill_manager").enabled:
        all_tools = [*all_tools, "Skill"]
    deferred_tools = effective_deferred_tools(role)
    if deferred_tools:
        all_tools = [*all_tools, "SearchTools"]
    tools_cfg = role.config.tools
    return ToolExecutor(
        session_id=role.state.session_id,
        tools=dedupe_tools(all_tools),
        role=role,
        tool_call_policy=ctx.dep("tool_call_policy"),
        tool_result_policy=ctx.dep("tool_result_policy"),
        limit_config=tools_cfg.result_limit,
        ledger_config=tools_cfg.effect_ledger,
        durable_config=tools_cfg.durable,
        loop_guard_config=tools_cfg.loop_guard,
        telemetry=ctx.dep("telemetry"),
        get_bg_pool=ctx.defer("bg_pool"),
        pipelines_enabled=role.config.context.bggraph.enabled,
        workspace_store=ctx.dep("workspace_store"),
        deferred_tools=deferred_tools,
        get_revealed=lambda: role.state.revealed_tools,
        toolsets=role.wiring.dependencies.toolsets,
        command_protocol=role.role_schema.command_protocol,
    )


__all__ = [
    "action_component_specs",
    "build_args_limiter",
    "dedupe_tools",
    "effective_deferred_tools",
]
