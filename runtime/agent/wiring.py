"""Immutable composition values inherited atomically by Agent incarnations."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Callable, Generic, Mapping, TypeVar

from mote.contracts.background_tasks import BackgroundTaskServiceFactory
from mote.contracts.leases import RunLeasePolicy
from mote.contracts.ports.agent_factory import AgentFactory
from mote.contracts.ports.compaction_policy import CompactionPolicyExtensionSpec
from mote.contracts.ports.prompt_policy import PromptPolicyExtensionSpec
from mote.contracts.ports.run_completion_policy import RunCompletionPolicyExtensionSpec
from mote.contracts.ports.tool_policy import ToolCallPolicyExtensionSpec
from mote.kernel.output import OutputContract, text_output_contract
from mote.kernel.tools.toolset import AnyToolset
from mote.runtime.models.clients.context import Context
from mote.runtime.services import EngineServices, EngineServicesLease

DepsT = TypeVar("DepsT")
OutputT = TypeVar("OutputT")


@dataclass(frozen=True, slots=True)
class AgentDependencies(Generic[DepsT, OutputT]):
    """Per-Agent immutable capabilities, policy, dependencies, and output type."""

    deps: DepsT
    output_contract: OutputContract[OutputT]
    run_lease_policy: RunLeasePolicy = field(default_factory=RunLeasePolicy)
    toolsets: tuple[AnyToolset, ...] = ()
    tool_policy_extensions: tuple[ToolCallPolicyExtensionSpec, ...] = ()
    prompt_policy_extensions: tuple[PromptPolicyExtensionSpec, ...] = ()
    compaction_policy_extensions: tuple[CompactionPolicyExtensionSpec, ...] = ()
    run_completion_policy_extensions: tuple[RunCompletionPolicyExtensionSpec, ...] = ()
    agent_factory: AgentFactory | None = None
    background_task_pool_builder: BackgroundTaskServiceFactory | None = None
    routing_strategy_builders: Mapping[str, Callable[[], object]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "toolsets", tuple(self.toolsets))
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
    ) -> "AgentWiring[Any, Any]":
        """Provision a complete dependency value with one Context.

        Omitting ``dependencies`` intentionally selects the text-output Agent
        specialization. Advanced callers construct :class:`AgentDependencies`
        first so adding a future capability never expands this boundary.
        """

        if dependencies is not None and deps is not None:
            raise ValueError("'dependencies' and 'deps' are mutually exclusive")
        resolved = dependencies if dependencies is not None else AgentDependencies.text(deps)
        services = EngineServices(context=context)
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
        return AgentWiring(dependencies=AgentDependencies.text(None))


__all__ = ["AgentDependencies", "AgentWiring"]
