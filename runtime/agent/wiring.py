"""Immutable composition values inherited atomically by Agent incarnations."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Generic, TypeVar, overload

from mote.contracts.runtime.application import ApplicationCompositionPort
from mote.contracts.session.lease import RunLeasePolicy
from mote.kernel.output import OutputContract, text_output_contract
from mote.runtime.models.clients.context import Context
from mote.runtime.services import EngineServices, EngineServicesLease

DepsT = TypeVar("DepsT")
OutputT = TypeVar("OutputT")
NewOutputT = TypeVar("NewOutputT")

if TYPE_CHECKING:
    from mote.runtime.agent.component_projection import AgentComponentProjection


@dataclass(frozen=True, slots=True)
class AgentDependencies(Generic[DepsT, OutputT]):
    """Per-Agent immutable capabilities, policy, dependencies, and output type."""

    deps: DepsT
    output_contract: OutputContract[OutputT]
    component_projection: "AgentComponentProjection | None" = None
    run_lease_policy: RunLeasePolicy = field(default_factory=RunLeasePolicy)

    def with_output_contract(
        self, output_contract: OutputContract[NewOutputT]
    ) -> "AgentDependencies[DepsT, NewOutputT]":
        """Keep capabilities while selecting an explicit output specialization."""
        return AgentDependencies(
            deps=self.deps,
            output_contract=output_contract,
            component_projection=self.component_projection,
            run_lease_policy=self.run_lease_policy,
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
    @overload
    def for_context(
        cls,
        context: Context,
        *,
        dependencies: AgentDependencies[DepsT, OutputT],
        deps: None = None,
        owned: bool = False,
        application_composition: ApplicationCompositionPort | None = None,
    ) -> "AgentWiring[DepsT, OutputT]": ...

    @classmethod
    @overload
    def for_context(
        cls,
        context: Context,
        *,
        dependencies: None = None,
        deps: DepsT,
        owned: bool = False,
        application_composition: ApplicationCompositionPort | None = None,
    ) -> "AgentWiring[DepsT, str]": ...

    @classmethod
    @overload
    def for_context(
        cls,
        context: Context,
        *,
        dependencies: None = None,
        deps: None = None,
        owned: bool = False,
        application_composition: ApplicationCompositionPort | None = None,
    ) -> "AgentWiring[None, str]": ...

    @classmethod
    def for_context(
        cls,
        context: Context,
        *,
        dependencies: AgentDependencies[DepsT, OutputT] | None = None,
        deps: DepsT | None = None,
        owned: bool = False,
        application_composition: ApplicationCompositionPort | None = None,
    ) -> "AgentWiring[DepsT, OutputT] | AgentWiring[DepsT, str] | AgentWiring[None, str]":
        """Provision a complete dependency value with one Context.

        Omitting ``dependencies`` intentionally selects the text-output Agent
        specialization. Advanced callers construct :class:`AgentDependencies`
        first so adding a future capability never expands this boundary.
        """

        if dependencies is not None and deps is not None:
            raise ValueError("'dependencies' and 'deps' are mutually exclusive")
        services = EngineServices(context=context, application_composition=application_composition)
        if dependencies is None:
            return AgentWiring(
                services=services,
                dependencies=AgentDependencies.text(deps),
                services_lease=services.acquire() if owned else None,
            )
        return AgentWiring(
            services=services,
            dependencies=dependencies,
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


__all__ = [
    "AgentDependencies",
    "AgentWiring",
]
