"""Immutable composition values inherited atomically by Agent incarnations."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Generic, Mapping, TypeVar

from mote.contracts.ports.code_intelligence.code_map import CodeMapIndexerFactory
from mote.contracts.ports.code_intelligence.lsp import LspServiceFactory
from mote.contracts.ports.conversation.compaction_policy import CompactionPolicyExtensionSpec
from mote.contracts.ports.conversation.prompt_policy import PromptPolicyExtensionSpec
from mote.contracts.ports.output.run_completion_policy import RunCompletionPolicyExtensionSpec
from mote.contracts.ports.skill.registry import SkillServiceFactory
from mote.contracts.ports.task.operations import BackgroundTaskServiceFactory
from mote.contracts.ports.tool.policy import ToolCallPolicyExtensionSpec
from mote.contracts.runtime.application import ApplicationCompositionPort
from mote.contracts.session.lease import RunLeasePolicy
from mote.kernel.output import OutputContract, text_output_contract
from mote.runtime.models.clients.context import Context
from mote.runtime.services import EngineServices, EngineServicesLease
from mote.runtime.tools.provider import NativeToolset, XmlToolset

DepsT = TypeVar("DepsT")
OutputT = TypeVar("OutputT")


@dataclass(frozen=True, slots=True)
class AgentDependencies(Generic[DepsT, OutputT]):
    """Per-Agent immutable capabilities, policy, dependencies, and output type."""

    deps: DepsT
    output_contract: OutputContract[OutputT]
    run_lease_policy: RunLeasePolicy = field(default_factory=RunLeasePolicy)
    toolsets: tuple[XmlToolset[DepsT] | NativeToolset[DepsT], ...] = ()
    tool_policy_extensions: tuple[ToolCallPolicyExtensionSpec, ...] = ()
    prompt_policy_extensions: tuple[PromptPolicyExtensionSpec, ...] = ()
    compaction_policy_extensions: tuple[CompactionPolicyExtensionSpec, ...] = ()
    run_completion_policy_extensions: tuple[RunCompletionPolicyExtensionSpec, ...] = ()
    skill_service_factory: SkillServiceFactory | None = None
    code_map_indexer_factory: CodeMapIndexerFactory | None = None
    hook_config: Any = None
    mcp_servers: tuple[Any, ...] = ()
    primary_config_path: Path | None = None
    config_secret_predicate: Callable[[str], bool] | None = None
    watched_config_files: tuple[Path, ...] = ()
    user_config_root: Path | None = None
    session_workspace_root: Path | None = None
    browser_profiles_root: Path | None = None
    sandbox_ca_root: Path | None = None
    secrets_root: Path | None = None
    oauth_root: Path | None = None
    lsp_service_factory: LspServiceFactory | None = None
    background_task_pool_builder: BackgroundTaskServiceFactory | None = None
    routing_strategy_builders: Mapping[str, Callable[[], object]] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        object.__setattr__(self, "toolsets", tuple(self.toolsets))
        object.__setattr__(self, "watched_config_files", tuple(self.watched_config_files))
        object.__setattr__(self, "mcp_servers", tuple(self.mcp_servers))
        object.__setattr__(
            self,
            "tool_policy_extensions",
            tuple(self.tool_policy_extensions),
        )
        object.__setattr__(
            self,
            "prompt_policy_extensions",
            tuple(self.prompt_policy_extensions),
        )
        object.__setattr__(
            self,
            "compaction_policy_extensions",
            tuple(self.compaction_policy_extensions),
        )
        object.__setattr__(
            self,
            "run_completion_policy_extensions",
            tuple(self.run_completion_policy_extensions),
        )
        object.__setattr__(
            self,
            "routing_strategy_builders",
            MappingProxyType(dict(self.routing_strategy_builders)),
        )

    @classmethod
    def text(cls, deps: DepsT) -> "AgentDependencies[DepsT, str]":
        """Build the explicitly text-output specialization."""

        return AgentDependencies(deps=deps, output_contract=text_output_contract())


@dataclass(frozen=True, slots=True)
class AgentWiring(Generic[DepsT, OutputT]):
    """Atomic pairing of borrowed Engine services and an Agent definition."""

    dependencies: AgentDependencies[DepsT, OutputT]
    services: EngineServices | None = None
    services_lease: EngineServicesLease | None = None

    def with_services(self, services: EngineServices, *, owned: bool = False) -> "AgentWiring[DepsT, OutputT]":
        if self.services_lease is not None:
            raise RuntimeError("AgentWiring already owns an EngineServices lease")
        return replace(
            self,
            services=services,
            services_lease=services.acquire() if owned else None,
        )

    def for_incarnation(self) -> "AgentWiring[DepsT, OutputT]":
        """Inherit dependencies and acquire distinct isolated-service ownership."""

        if self.services_lease is None:
            return self
        if self.services is None:
            raise RuntimeError("owned AgentWiring has no EngineServices")
        return replace(self, services_lease=self.services.acquire())

    @classmethod
    def for_context(
        cls,
        context: Context,
        *,
        dependencies: AgentDependencies[DepsT, OutputT] | None = None,
        deps: DepsT | None = None,
        owned: bool = False,
        application_composition: ApplicationCompositionPort | None = None,
    ) -> "AgentWiring[Any, Any]":
        """Provision a complete dependency value with one Context.

        Omitting ``dependencies`` intentionally selects the text-output Agent
        specialization. Advanced callers construct :class:`AgentDependencies`
        first so adding a future capability never expands this boundary.
        """

        if dependencies is not None and deps is not None:
            raise ValueError("'dependencies' and 'deps' are mutually exclusive")
        resolved = dependencies if dependencies is not None else replace(cls.defaults().dependencies, deps=deps)
        services = EngineServices(context=context, application_composition=application_composition)
        return AgentWiring(
            services=services,
            dependencies=resolved,
            services_lease=services.acquire() if owned else None,
        )

    @classmethod
    def for_dependencies(
        cls,
        dependencies: AgentDependencies[DepsT, OutputT],
    ) -> "AgentWiring[DepsT, OutputT]":
        """Build an unprovisioned wiring value for a later spawn boundary."""

        return AgentWiring(dependencies=dependencies)

    @classmethod
    def defaults(cls) -> "AgentWiring[None, str]":
        return AgentWiring(dependencies=AgentDependencies[None, str].text(None))


__all__ = ["AgentDependencies", "AgentWiring"]
