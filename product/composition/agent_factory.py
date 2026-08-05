"""Canonical construction of Product root Agent incarnations."""

from __future__ import annotations

from pathlib import Path

from mote.kernel.output import text_output_contract
from mote.product.agents.catalog import AgentCatalog
from mote.product.agents.defaults import DEFAULT_DEFERRED_TOOLS, DEFAULT_TOOLS
from mote.product.agents.factory import CodingAgentFactory, RootAgentRequest
from mote.product.config.adapters.mcp import MCP_CONFIG_FILE_NAME, load_mcp_servers
from mote.product.config.adapters.permissions import build_product_permission_config, load_permission_rules
from mote.product.extensions.sources import ExtensionKind, ExtensionSourcePolicy
from mote.product.paths import RuntimePaths, mote_layered_files
from mote.runtime.agent import Role
from mote.runtime.agent.role_schema import RoleSchema
from mote.runtime.agent.role_state import RoleState
from mote.runtime.agent.wiring import AgentWiring
from mote.runtime.engine import ClosableAgent
from mote.runtime.file_watch.config import FileWatchConfig
from mote.runtime.services import EngineServices
from mote.runtime.vcs import find_git_root


def _apply_cwd(state: RoleState, cwd: Path | None) -> None:
    if cwd is None:
        return
    state.working_dir = str(cwd)
    state.original_working_dir = str(cwd)
    state.project_root = find_git_root(str(cwd)) or str(cwd)


def _approved_mcp_names(
    cwd: Path | None,
    paths: RuntimePaths,
    source_policy: ExtensionSourcePolicy,
) -> list[str]:
    files = mote_layered_files("mcp.json", cwd, user_config_root=paths.user_config_root)
    approved = source_policy.admitted_files(ExtensionKind.MCP, files)
    return [server.name for server in load_mcp_servers(approved)]


def _file_watch_roots(cwd: Path | None) -> list[str]:
    base = cwd or Path.cwd()
    return [str(base / ".mote" / MCP_CONFIG_FILE_NAME)]


def build_product_agent(
    *,
    services: EngineServices,
    agent_factory: CodingAgentFactory,
    agent_catalog: AgentCatalog[str],
    paths: RuntimePaths,
    source_policy: ExtensionSourcePolicy,
    name: str,
    tools: tuple[str, ...] | None = None,
    cwd: Path | None = None,
    agent_type: str | None = None,
    session_id: str | None = None,
) -> ClosableAgent:
    """Build one root Agent from the owning Application snapshots."""

    if agent_type:
        root_agent_type = agent_catalog.agent_type(agent_type)
        if root_agent_type is None:
            raise ValueError(f"unknown Agent type {agent_type!r}")
        schema = RoleSchema(
            tools=list(DEFAULT_TOOLS),
            deferred_tools=list(DEFAULT_DEFERRED_TOOLS),
        )
        wiring = AgentWiring(
            services=services,
            dependencies=agent_factory.dependencies(
                deps=None,
                output_contract=text_output_contract(),
                command_protocol=schema.command_protocol,
            ),
        )
        state = RoleState(parent_session_id=None)
        _apply_cwd(state, cwd)
        # AgentCatalog admitted this class through its BaseRole validation, but
        # its current public return type cannot preserve the constructor shape.
        # R2.15 owns that end-to-end generic contract repair.
        role = agent_factory.root_builder(root_agent_type).build(  # type: ignore[reportArgumentType]
            RootAgentRequest(
                role_schema=schema,
                state=state,
                wiring=wiring,
            )
        )
    else:
        permissions = build_product_permission_config(
            load_permission_rules(
                mote_layered_files("settings.local.json", cwd, user_config_root=paths.user_config_root)
            )
        )
        file_watch = FileWatchConfig(
            enabled=True,
            roots=_file_watch_roots(cwd),
            reload_mcp=True,
            reload_skills=True,
        )
        mcps = _approved_mcp_names(cwd, paths, source_policy)
        if tools:
            schema = RoleSchema(
                name=name,
                mcps=mcps,
                file_watch=file_watch,
                permissions=permissions,
                tools=list(tools),
            )
        else:
            schema = RoleSchema(
                name=name,
                mcps=mcps,
                file_watch=file_watch,
                permissions=permissions,
                tools=list(DEFAULT_TOOLS),
                deferred_tools=list(DEFAULT_DEFERRED_TOOLS),
            )
        state = RoleState(session_id=session_id) if session_id else RoleState()
        _apply_cwd(state, cwd)
        wiring = AgentWiring(
            services=services,
            dependencies=agent_factory.dependencies(
                deps=None,
                output_contract=text_output_contract(),
                command_protocol=schema.command_protocol,
            ),
        )
        role = agent_factory.root_builder(Role).build(
            RootAgentRequest(name=name, role_schema=schema, state=state, wiring=wiring)
        )
    return role


__all__ = ["build_product_agent"]
