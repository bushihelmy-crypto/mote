"""The standard Product composition root for Coding Agents."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Generic, Mapping, Protocol, TypeVar

from mote.contracts.agent import AgentBuilder, AgentConstructionRequest, ApprovedDeclaration, RunnableAgent
from mote.contracts.model.checkpoint import ModelCheckpointPolicy
from mote.contracts.ports.agent.composition import RoutingStrategyFactory
from mote.contracts.ports.conversation.compaction_policy import CompactionPolicyExtensionSpec
from mote.contracts.ports.conversation.prompt_policy import PromptPolicyExtensionSpec
from mote.contracts.ports.model.routing import RoutingPolicy
from mote.contracts.ports.output.run_completion_policy import RunCompletionPolicyExtensionSpec
from mote.contracts.ports.task.operations import BackgroundTaskServiceFactory
from mote.contracts.ports.tool.deferred import DeferredResultProjectorFactory
from mote.contracts.ports.tool.policy import ToolCallPolicyExtensionSpec
from mote.contracts.tool import CommandProtocol
from mote.kernel.output import OutputContract, text_output_contract
from mote.product.agents.background_tasks import build_background_task_pool
from mote.product.agents.defaults import DEFAULT_DEFERRED_TOOLS, DEFAULT_TOOLS
from mote.product.agents.deferred_projection import build_deferred_result_projector
from mote.product.agents.output_publication import ProductOutputPublisherFactory
from mote.product.code_map import ProductCodeMapIndexerFactory
from mote.product.config.schema import Config
from mote.product.extensions.sources import ExtensionSourcePolicy
from mote.product.lsp.factory import ProductLspServiceFactory
from mote.product.paths import RuntimePaths, default_runtime_paths
from mote.product.skills import ProductSkillServiceFactory
from mote.product.toolsets import builtin_toolsets
from mote.runtime.agent.component_projection import AgentComponentProjection
from mote.runtime.agent.components.action import ActionComponentInputs
from mote.runtime.agent.components.cognition import CognitionComponentInputs
from mote.runtime.agent.components.context import ContextComponentInputs
from mote.runtime.agent.components.integrations import IntegrationComponentInputs
from mote.runtime.agent.components.policy import PolicyComponentInputs
from mote.runtime.agent.components.session import SessionComponentInputs
from mote.runtime.agent.components.watching import WatchingComponentInputs
from mote.runtime.agent.role_schema import RoleSchema
from mote.runtime.agent.role_state import RoleState
from mote.runtime.agent.wiring import AgentDependencies, AgentWiring
from mote.runtime.config.hook import HookConfig
from mote.runtime.config.mcp import MCPServerConfig
from mote.runtime.tools.provider import ContextFreeToolset

DepsT = TypeVar("DepsT")
OutputT = TypeVar("OutputT")
AgentT = TypeVar("AgentT")


@dataclass(frozen=True, slots=True)
class _ProductRoutingStrategyFactory(RoutingStrategyFactory):
    builders: Mapping[str, Callable[[], RoutingPolicy]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "builders", MappingProxyType(dict(self.builders)))

    def build(self, name: str) -> RoutingPolicy | None:
        builder = self.builders.get(name)
        return builder() if builder is not None else None


@dataclass(frozen=True, slots=True)
class _ProductAgentComponentProjection(AgentComponentProjection):
    action_inputs: ActionComponentInputs
    cognition_inputs: CognitionComponentInputs
    context_inputs: ContextComponentInputs
    integration_inputs: IntegrationComponentInputs
    policy_inputs: PolicyComponentInputs
    session_inputs: SessionComponentInputs
    watching_inputs: WatchingComponentInputs
    config_root: Path
    workspace_root: Path

    def action(self) -> ActionComponentInputs:
        return self.action_inputs

    def cognition(self) -> CognitionComponentInputs:
        return replace(self.cognition_inputs, component_projection=self)

    def context(self) -> ContextComponentInputs:
        return self.context_inputs

    def integrations(self) -> IntegrationComponentInputs:
        return self.integration_inputs

    def policy(self) -> PolicyComponentInputs:
        return self.policy_inputs

    def session(self) -> SessionComponentInputs:
        return self.session_inputs

    def watching(self) -> WatchingComponentInputs:
        return self.watching_inputs

    def watched_config_paths(self) -> list[str]:
        return [str(path) for path in self.watching_inputs.watched_config_files]

    def user_config_root(self) -> Path:
        return self.config_root

    def session_workspace_root(self) -> Path:
        return self.workspace_root


class _RootAgentClass(Protocol[DepsT, OutputT]):
    def __call__(
        self,
        *,
        name: str | None,
        role_schema: RoleSchema,
        state: RoleState,
        wiring: AgentWiring[DepsT, OutputT],
        config: Config | None,
    ) -> RunnableAgent[OutputT]: ...


class _ChildAgentClass(Protocol):
    def __call__(
        self,
        *,
        state: RoleState,
        wiring: AgentWiring[None, str],
        config: Config | None,
    ) -> RunnableAgent[str]: ...


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
    config: Config | None

    def build(self, request: RootAgentRequest[DepsT, OutputT]) -> RunnableAgent[OutputT]:
        return self.agent_cls(
            name=request.name,
            role_schema=request.role_schema,
            state=request.state,
            wiring=request.wiring,
            config=self.config,
        )


@dataclass(frozen=True, slots=True)
class _ChildBuilder:
    factory: "CodingAgentFactory"
    agent_cls: _ChildAgentClass

    def build(self, request: AgentConstructionRequest) -> RunnableAgent[str]:
        return self.factory.construct_child(self.agent_cls, request)


class CodingAgentFactory:
    """Construct Runtime Agents with the complete Product capability set."""

    def __init__(
        self,
        *,
        model_checkpoint_policy: ModelCheckpointPolicy,
        toolsets_factory: Callable[[str | CommandProtocol], tuple[ContextFreeToolset, ...]] = builtin_toolsets,
        background_task_pool_builder: BackgroundTaskServiceFactory = build_background_task_pool,
        deferred_result_projector_factory: DeferredResultProjectorFactory = build_deferred_result_projector,
        routing_strategy_builders_factory: Callable[[], dict[str, Callable[[], RoutingPolicy]]] = dict,
        skill_service_factory: ProductSkillServiceFactory | None = None,
        code_map_indexer_factory: ProductCodeMapIndexerFactory | None = None,
        paths: RuntimePaths | None = None,
        cwd: Path | None = None,
        watched_config_files: tuple[Path, ...] = (),
        hooks: ApprovedDeclaration[HookConfig] | None = None,
        mcp: ApprovedDeclaration[tuple[MCPServerConfig, ...]] | None = None,
        primary_config_path: Path | None = None,
        config_secret_predicate: Callable[[str], bool] | None = None,
        user_config_root: Path | None = None,
        session_workspace_root: Path | None = None,
        browser_profiles_root: Path | None = None,
        sandbox_ca_root: Path | None = None,
        secrets_root: Path | None = None,
        oauth_root: Path | None = None,
        lsp_service_factory: ProductLspServiceFactory | None = None,
        tool_policy_extensions: tuple[ToolCallPolicyExtensionSpec, ...] = (),
        prompt_policy_extensions: tuple[PromptPolicyExtensionSpec, ...] = (),
        compaction_policy_extensions: tuple[CompactionPolicyExtensionSpec, ...] = (),
        run_completion_policy_extensions: tuple[RunCompletionPolicyExtensionSpec, ...] = (),
        config: Config | None = None,
    ) -> None:
        self._config = config
        self._toolsets_factory = toolsets_factory
        self._model_checkpoint_policy = model_checkpoint_policy
        self._background_task_pool_builder = background_task_pool_builder
        self._deferred_result_projector_factory = deferred_result_projector_factory
        self._routing_strategy_builders_factory = routing_strategy_builders_factory
        default_paths = paths or default_runtime_paths()
        self._cwd = cwd
        self._skill_service_factory = skill_service_factory or ProductSkillServiceFactory(
            default_paths.user_config_root,
            source_policy=ExtensionSourcePolicy(
                user_root=default_paths.user_config_root,
                builtin_roots=(default_paths.package_data_root,),
            ),
        )
        self._code_map_indexer_factory = code_map_indexer_factory or ProductCodeMapIndexerFactory(
            codemap_root=default_paths.codemap_root
        )
        self._watched_config_files = tuple(watched_config_files)
        self._hooks = hooks
        self._mcp = mcp
        self._primary_config_path = primary_config_path
        self._config_secret_predicate = config_secret_predicate
        self._user_config_root = user_config_root or default_paths.user_config_root
        self._session_workspace_root = session_workspace_root or default_paths.session_workspace_root
        self._browser_profiles_root = browser_profiles_root or default_paths.browser_profiles_root
        self._sandbox_ca_root = sandbox_ca_root or default_paths.sandbox_ca_root
        self._secrets_root = secrets_root or default_paths.secrets_root
        self._oauth_root = oauth_root or default_paths.oauth_root
        self._lsp_service_factory = lsp_service_factory or ProductLspServiceFactory()
        self._tool_policy_extensions = tuple(tool_policy_extensions)
        self._prompt_policy_extensions = tuple(prompt_policy_extensions)
        self._compaction_policy_extensions = tuple(compaction_policy_extensions)
        self._run_completion_policy_extensions = tuple(run_completion_policy_extensions)

    def dependencies(
        self,
        *,
        deps: DepsT,
        output_contract: OutputContract[OutputT],
        toolsets: tuple[ContextFreeToolset, ...] | None = None,
        command_protocol: str | CommandProtocol = CommandProtocol.NATIVE,
    ) -> AgentDependencies[DepsT, OutputT]:
        """Build the complete immutable Product dependency definition."""

        resolved_toolsets = toolsets if toolsets is not None else self._toolsets_factory(command_protocol)
        routing_factory = _ProductRoutingStrategyFactory(self._routing_strategy_builders_factory())
        projection = _ProductAgentComponentProjection(
            action_inputs=ActionComponentInputs(
                session_workspace_root=self._session_workspace_root,
                secrets_root=self._secrets_root,
                browser_profiles_root=self._browser_profiles_root,
                oauth_root=self._oauth_root,
                toolsets=resolved_toolsets,
                tool_policy_extensions=self._tool_policy_extensions,
                background_task_pool_builder=self._background_task_pool_builder,
                deferred_result_projector_factory=self._deferred_result_projector_factory,
                mcp_servers=self._mcp.value if self._mcp is not None else (),
            ),
            cognition_inputs=CognitionComponentInputs(
                routing_factory,
                model_checkpoint_policy=self._model_checkpoint_policy,
            ),
            context_inputs=ContextComponentInputs(
                skill_service_factory=self._skill_service_factory,
                code_map_indexer_factory=self._code_map_indexer_factory,
                compaction_policy_extensions=self._compaction_policy_extensions,
            ),
            integration_inputs=IntegrationComponentInputs(
                approved_hooks=self._hooks,
                lsp_service_factory=self._lsp_service_factory,
                secrets_root=self._secrets_root,
                browser_profiles_root=self._browser_profiles_root,
                sandbox_ca_root=self._sandbox_ca_root,
                primary_config_path=self._primary_config_path,
                config_secret_predicate=self._config_secret_predicate,
            ),
            policy_inputs=PolicyComponentInputs(
                prompt_extensions=self._prompt_policy_extensions,
                completion_extensions=self._run_completion_policy_extensions,
            ),
            session_inputs=SessionComponentInputs(
                self._secrets_root,
                ProductOutputPublisherFactory(self._session_workspace_root),
            ),
            watching_inputs=WatchingComponentInputs(self._watched_config_files, self._hooks),
            config_root=self._user_config_root,
            workspace_root=self._session_workspace_root,
        )
        return AgentDependencies(
            deps=deps,
            output_contract=output_contract,
            component_projection=projection,
        )

    def root_builder(
        self, agent_cls: _RootAgentClass[DepsT, OutputT], /
    ) -> AgentBuilder[RootAgentRequest[DepsT, OutputT], OutputT]:
        return _RootBuilder(agent_cls, self._config)

    def child_builder(self, agent_cls: _ChildAgentClass, /) -> AgentBuilder[AgentConstructionRequest, str]:
        return _ChildBuilder(self, agent_cls)

    def construct_child(self, agent_cls: _ChildAgentClass, request: AgentConstructionRequest) -> RunnableAgent[str]:
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
            state=RoleState(
                session_id=request.logical_agent_id,
                parent_session_id=request.parent_session_id,
            ),
            wiring=wiring,
            config=self._config,
        )
        if not isinstance(agent, RunnableAgent):
            raise TypeError(f"agent factory returned non-runnable {type(agent).__name__!r}")
        return agent


__all__ = ["CodingAgentFactory", "RootAgentRequest"]
