"""The standard Product composition root for Coding Agents."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from mote.contracts.background_tasks import BackgroundTaskServiceFactory
from mote.contracts.tools import CommandProtocol
from mote.kernel.output import OutputContract, text_output_contract
from mote.kernel.tools.toolset import AnyToolset
from mote.orchestration.tasks import build_background_task_pool
from mote.product.toolsets import builtin_toolsets
from mote.runtime.agent.wiring import AgentDependencies, AgentWiring
from mote.runtime.services import EngineServices

DepsT = TypeVar("DepsT")
OutputT = TypeVar("OutputT")
AgentT = TypeVar("AgentT")


class CodingAgentFactory:
    """Construct Runtime Agents with the complete Product capability set."""

    def __init__(
        self,
        *,
        toolsets_factory: Callable[[str | CommandProtocol], tuple[AnyToolset, ...]] = builtin_toolsets,
        background_task_pool_builder: BackgroundTaskServiceFactory = build_background_task_pool,
        routing_strategy_builders_factory: Callable[[], dict] = dict,
    ) -> None:
        self._toolsets_factory = toolsets_factory
        self._background_task_pool_builder = background_task_pool_builder
        self._routing_strategy_builders_factory = routing_strategy_builders_factory

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
            agent_factory=self,
            background_task_pool_builder=self._background_task_pool_builder,
            routing_strategy_builders=self._routing_strategy_builders_factory(),
        )

    def build(self, agent_cls: type[AgentT], /, **kwargs: Any) -> AgentT:
        wiring = kwargs.pop("wiring", None)
        services = kwargs.pop("services", None)
        dependencies = kwargs.pop("dependencies", None)
        deps = kwargs.pop("deps", None)
        output_contract = kwargs.pop("output_contract", None)
        if "context" in kwargs:
            raise ValueError("Context is not an Agent dependency; pass EngineServices through 'services'.")
        if wiring is not None and any(value is not None for value in (services, dependencies, deps, output_contract)):
            raise ValueError("'wiring' is mutually exclusive with services, dependencies, deps, and output_contract.")
        if wiring is None:
            if services is not None and not isinstance(services, EngineServices):
                raise TypeError("services must be an EngineServices instance")
            if dependencies is None:
                dependencies = self.dependencies(
                    deps=deps,
                    output_contract=output_contract or text_output_contract(),
                    command_protocol=getattr(kwargs.get("role_schema"), "command_protocol", "native"),
                )
            wiring = AgentWiring(
                services=services,
                dependencies=dependencies,
            )
        kwargs["wiring"] = wiring
        return agent_cls(**kwargs)


__all__ = ["CodingAgentFactory"]
