"""Tool execution, command protocol, and background-action manifest."""
from __future__ import annotations

from typing import Any

from mote.common.resource import build_task_result_pointer
from mote.common.schema import PAUSE_STATUSES, TERMINAL_STATUSES, BgStatus
from mote.common.workspace import WorkspaceStore
from mote.executor.tasks import BackgroundTaskPool, TaskOutputStore
from mote.executor.tool_executor import ToolExecutor
from mote.parser import infer_native_tool_provider, make_command_channel
from mote.roles.component_graph import ComponentSpec


def action_component_specs() -> list[ComponentSpec]:
    return [
        ComponentSpec("workspace_store", lambda ctx: WorkspaceStore()),
        ComponentSpec("bg_pool", _build_bg_pool),
        ComponentSpec("executor", _build_executor),
        ComponentSpec("command_channel", _build_command_channel),
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


def _build_bg_pool(ctx) -> BackgroundTaskPool:
    role = ctx.role
    output_store = TaskOutputStore(session_id=role.state.session_id, store=ctx.dep("workspace_store"))
    pool = BackgroundTaskPool(
        msg_buffer=role.state.msg_buffer,
        output_store=output_store,
        wake=ctx.state.pending_task_completion_wake,
        session_id=role.state.session_id,
    )
    output_store.set_on_cap(pool.cancel_for_cap)

    def _on_terminal(meta) -> None:
        status_value = meta.status.value if isinstance(meta.status, BgStatus) else str(meta.status)
        if meta.status in PAUSE_STATUSES:
            content = build_task_result_pointer(
                task_id=meta.task_id,
                command_name=meta.command_name,
                status=status_value,
                summary=f"{meta.command_name} paused ({status_value}), awaiting a decision.",
            )
        elif meta.status in TERMINAL_STATUSES:
            content = build_task_result_pointer(
                task_id=meta.task_id,
                command_name=meta.command_name,
                status=status_value,
                summary=f"{meta.command_name} finished ({status_value}).",
                result=meta.result,
                output_path=meta.output_path,
            )
        else:
            return
        role._capabilities.register_task_result(meta.task_id, content)
        meta.registered_resource = True

    pool.set_on_terminal_result(_on_terminal)
    pool.set_retire_result(role.resource_registry.unload)
    return pool


def _build_command_channel(ctx):
    return make_command_channel(
        ctx.role.role_schema.command_protocol,
        provider=infer_native_tool_provider(ctx.role.config.models.default),
        model=getattr(ctx.role.config.models.default, "model", None),
        args_limiter=build_args_limiter(ctx.dep("executor")),
    )


def _build_browser_profile_store(ctx):
    from mote.common.secrets.cipher import build_cipher
    from mote.executor.dependency.browser_profile import BrowserProfileStore

    secrets_cfg = ctx.role.config.secrets
    return BrowserProfileStore(lambda: build_cipher(secrets_cfg))


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
        permission_config=role.role_schema.permissions,
        limit_config=tools_cfg.result_limit,
        ledger_config=tools_cfg.effect_ledger,
        durable_config=tools_cfg.durable,
        loop_guard_config=tools_cfg.loop_guard,
        bus=ctx.dep("event_bus"),
        get_bg_pool=ctx.defer("bg_pool"),
        pipelines_enabled=role.config.context.bggraph.enabled,
        workspace_store=ctx.dep("workspace_store"),
        deferred_tools=deferred_tools,
        get_revealed=lambda: role.state.revealed_tools,
    )


__all__ = [
    "action_component_specs",
    "build_args_limiter",
    "dedupe_tools",
    "effective_deferred_tools",
]
