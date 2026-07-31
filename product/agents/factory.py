"""The standard Product composition root for Coding Agents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generic, Protocol, TypeGuard, TypeVar

from mote.contracts.agent import AgentBuilder, AgentConstructionRequest, RunnableAgent, is_text_runnable_agent
from mote.contracts.ports.task.operations import BackgroundTaskServiceFactory
from mote.contracts.tool import CommandProtocol
from mote.kernel.output import OutputContract, text_output_contract
from mote.product.agents.background_tasks import build_background_task_pool
from mote.product.agents.defaults import DEFAULT_DEFERRED_TOOLS, DEFAULT_TOOLS
from mote.product.code_map import ProductCodeMapIndexerFactory
from mote.product.lsp.factory import ProductLspServiceFactory
from mote.product.paths import RuntimePaths, default_runtime_paths
from mote.product.skills import ProductSkillServiceFactory
from mote.product.toolsets import builtin_toolsets
from mote.runtime.agent.role_schema import RoleSchema
from mote.runtime.agent.role_state import RoleState
from mote.runtime.agent.wiring import AgentDependencies, AgentWiring
from mote.runtime.tools.provider import AnyToolset

DepsT = TypeVar("DepsT")
OutputT = TypeVar("OutputT")
AgentT = TypeVar("AgentT")


class _RootAgentClass(Protocol[DepsT, OutputT]):
    def __call__(
        self,
        *,
        name: str | None,
        role_schema: RoleSchema,
        state: RoleState,
        wiring: AgentWiring[DepsT, OutputT],
    ) -> RunnableAgent[OutputT]:
        ...


class _ChildAgentClass(Protocol):
    def __call__(
        self,
        *,
        parent_session_id: str | None,
        wiring: AgentWiring[None, str],
    ) -> RunnableAgent[str]:
        ...


def _is_child_agent_class(candidate: object) -> TypeGuard[_ChildAgentClass]:
    return isinstance(candidate, type) and callable(candidate)


@dataclass(frozen=True, slots=True)
class RootAgentRequest(Generic[DepsT, OutputT]):
    """Explicit Product-only inputs for one application root Agent."""

    role_schema: RoleSchema
    state: RoleState
    wiring: AgentWiring[DepsT, OutputT]
    name: str | None = None


@dataclass(frozen=True, slots=True)
class _RootBuilder(Generic[DepsT, OutputT]):
    agent_cls: _RootAgentClass[DepsT, OutputT]

    def build(self, request: RootAgentRequest[DepsT, OutputT]) -> RunnableAgent[OutputT]:
        return self.agent_cls(
            name=request.name,
            role_schema=request.role_schema,
            state=request.state,
            wiring=request.wiring,
        )


@dataclass(frozen=True, slots=True)
class _ChildBuilder:
    factory: "CodingAgentFactory"
    agent_cls: object

    def build(self, request: AgentConstructionRequest) -> RunnableAgent[str]:
        agent = self.factory.construct_child(self.agent_cls, request)
        if not is_text_runnable_agent(agent):
            raise TypeError(f"agent factory returned non-runnable {type(agent).__name__!r}")
        return agent


class CodingAgentFactory:
    """Construct Runtime Agents with the complete Product capability set."""

    def __init__(
        self,
        *,
        toolsets_factory: Callable[[str | CommandProtocol], tuple[AnyToolset, ...]] = builtin_toolsets,
        background_task_pool_builder: BackgroundTaskServiceFactory = build_background_task_pool,
        routing_strategy_builders_factory: Callable[[], dict[str, Callable[[], object]]] = dict,
        skill_service_factory: ProductSkillServiceFactory | None = None,
        code_map_indexer_factory: ProductCodeMapIndexerFactory | None = None,
        paths: RuntimePaths | None = None,
        cwd: Path | None = None,
        watched_config_files: tuple[Path, ...] = (),
        hook_config: Any = None,
        mcp_servers: tuple[Any, ...] = (),
        primary_config_path: Path | None = None,
        config_secret_predicate: Callable[[str], bool] | None = None,
        user_config_root: Path | None = None,
        session_workspace_root: Path | None = None,
        browser_profiles_root: Path | None = None,
        sandbox_ca_root: Path | None = None,
        secrets_root: Path | None = None,
        oauth_root: Path | None = None,
        lsp_service_factory: ProductLspServiceFactory | None = None,
    ) -> None:
        self._toolsets_factory = toolsets_factory
        self._background_task_pool_builder = background_task_pool_builder
        self._routing_strategy_builders_factory = routing_strategy_builders_factory
        default_paths = paths or default_runtime_paths()
        self._cwd = cwd
        self._skill_service_factory = skill_service_factory or ProductSkillServiceFactory()
        self._code_map_indexer_factory = code_map_indexer_factory or ProductCodeMapIndexerFactory(
            codemap_root=default_paths.codemap_root
        )
        self._watched_config_files = tuple(watched_config_files)
        self._hook_config = hook_config
        self._mcp_servers = tuple(mcp_servers)
        self._primary_config_path = primary_config_path
        self._config_secret_predicate = config_secret_predicate
        self._user_config_root = user_config_root or default_paths.user_config_root
        self._session_workspace_root = session_workspace_root or default_paths.session_workspace_root
        self._browser_profiles_root = browser_profiles_root or default_paths.browser_profiles_root
        self._sandbox_ca_root = sandbox_ca_root or default_paths.sandbox_ca_root
        self._secrets_root = secrets_root or default_paths.secrets_root
        self._oauth_root = oauth_root or default_paths.oauth_root
        self._lsp_service_factory = lsp_service_factory or ProductLspServiceFactory()

    def dependencies(
        self,
        *,
        deps: DepsT,
        output_contract: OutputContract[OutputT],
        toolsets: tuple[AnyToolset, ...] | None = None,
        command_protocol: str | CommandProtocol = CommandProtocol.NATIVE,
    ) -> AgentDependencies[DepsT, OutputT]:
        """Build the complete immutable Product dependency definition."""

        return AgentDependencies(
            deps=deps,
            output_contract=output_contract,
            toolsets=(toolsets if toolsets is not None else self._toolsets_factory(command_protocol)),
            skill_service_factory=self._skill_service_factory,
            code_map_indexer_factory=self._code_map_indexer_factory,
            hook_config=self._hook_config,
            mcp_servers=self._mcp_servers,
            primary_config_path=self._primary_config_path,
            config_secret_predicate=self._config_secret_predicate,
            watched_config_files=self._watched_config_files,
            user_config_root=self._user_config_root,
            session_workspace_root=self._session_workspace_root,
            browser_profiles_root=self._browser_profiles_root,
            sandbox_ca_root=self._sandbox_ca_root,
            secrets_root=self._secrets_root,
            oauth_root=self._oauth_root,
            lsp_service_factory=self._lsp_service_factory,
            background_task_pool_builder=self._background_task_pool_builder,
            routing_strategy_builders=self._routing_strategy_builders_factory(),
        )

    def root_builder(
        self, agent_cls: _RootAgentClass[DepsT, OutputT], /
    ) -> AgentBuilder[RootAgentRequest[DepsT, OutputT], OutputT]:
        return _RootBuilder(agent_cls)

    def child_builder(self, agent_cls: object, /) -> AgentBuilder[AgentConstructionRequest, str]:
        return _ChildBuilder(self, agent_cls)

    def construct_child(self, agent_cls: object, request: AgentConstructionRequest) -> object:
        if not _is_child_agent_class(agent_cls):
            raise TypeError("child Agent declaration must be a constructible class")
        role_schema = RoleSchema(
            tools=list(DEFAULT_TOOLS),
            deferred_tools=list(DEFAULT_DEFERRED_TOOLS),
        )
        wiring = AgentWiring(
            dependencies=self.dependencies(
                deps=None,
                output_contract=text_output_contract(),
                command_protocol=role_schema.command_protocol,
            )
        )
        agent = agent_cls(
            parent_session_id=request.parent_session_id,
            wiring=wiring,
        )
        return agent


__all__ = ["CodingAgentFactory", "RootAgentRequest"]
