"""Public lifecycle and composition facade."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar, overload

from mote.agent import Agent
from mote.contracts.tools import CommandProtocol
from mote.kernel.output import OutputContract, text_output_contract
from mote.kernel.tools.toolset import AnyToolset
from mote.model import Model
from mote.product.application import Application
from mote.product.container import ProductContainer
from mote.product.integrations.bootstrap import builtin_model_gateway, builtin_service_gateway
from mote.runtime.agent import Role, RoleSchema
from mote.runtime.config.loader import load_config
from mote.runtime.engine import EngineState
from mote.runtime.models.clients.context import Context
from mote.runtime.models.failover import (
    LocalModelCallJournal,
    ResourceAdmissionController,
    default_model_call_journal_root,
)
from mote.runtime.service_gateway import LocalServiceCallJournal, default_service_call_journal_root
from mote.runtime.services import EngineServices
from mote.runtime.vcs import find_git_root

DepsT = TypeVar("DepsT")
OutputT = TypeVar("OutputT")


class Engine:
    """Own shared services and mint small typed Agent handles.

    Model/provider clients, durable writers and every Agent resource are closed
    by this async context manager. Provider factories, routing builders, leases
    and component graphs are Product composition details and are not constructor
    parameters.
    """

    def __init__(
        self,
        model: str | Model | None = None,
        *,
        cwd: str | Path | None = None,
        profile: str | None = None,
    ) -> None:
        selection = Model(model) if isinstance(model, str) else model
        root = Path(cwd).resolve() if cwd is not None else Path.cwd()
        config = load_config(
            root,
            profile=profile,
            programmatic=selection.config_overlay() if selection is not None else None,
        )
        if not config.models.default.model:
            raise ValueError("Engine requires a configured default model.")
        container = ProductContainer.standard(config)
        context = Context(config=config, provider_factory=container.providers.create)
        admission = ResourceAdmissionController(breaker_config=config.resilience.to_breaker_config())
        context.model_operator = admission
        context.model_gateway = builtin_model_gateway(
            config.models,
            providers=container.providers,
            cost_tracker=context.cost_manager,
            admission_controller=admission,
            model_call_journal=LocalModelCallJournal(default_model_call_journal_root()),
        )
        context.service_gateway = builtin_service_gateway(
            config.multimodal,
            config.tools.web_search,
            model_gateway=context.model_gateway,
            media_providers=container.media_providers,
            search_backends=container.search_backends,
            admission_controller=admission,
            service_call_journal=LocalServiceCallJournal(default_service_call_journal_root()),
        )
        services = EngineServices(context=context)
        self._cwd = str(root)
        self._runtime: Application[Role[Any, Any]] = Application(
            container=container,
            services=services,
            agent_factory=self._build_role,
        )

    @property
    def model(self) -> Model:
        configured = self._runtime.config.models.default
        return Model(
            name=configured.model or "",
            provider=configured.provider,
        )

    @overload
    def agent(
        self,
        *,
        deps: DepsT,
        output_contract: OutputContract[OutputT],
        name: str = "Assistant",
        tools: Sequence[str] | None = None,
        toolsets: Sequence[AnyToolset] | None = None,
        command_protocol: str | CommandProtocol = CommandProtocol.NATIVE,
    ) -> Agent[DepsT, OutputT]:
        ...

    @overload
    def agent(
        self,
        *,
        deps: DepsT = None,
        output_contract: None = None,
        name: str = "Assistant",
        tools: Sequence[str] | None = None,
        toolsets: Sequence[AnyToolset] | None = None,
        command_protocol: str | CommandProtocol = CommandProtocol.NATIVE,
    ) -> Agent[DepsT, str]:
        ...

    def agent(
        self,
        *,
        deps: Any = None,
        output_contract: OutputContract[Any] | None = None,
        name: str = "Assistant",
        tools: Sequence[str] | None = None,
        toolsets: Sequence[AnyToolset] | None = None,
        command_protocol: str | CommandProtocol = CommandProtocol.NATIVE,
    ) -> Agent[Any, Any]:
        """Create a typed Agent without exposing Runtime construction details."""

        contract = output_contract or text_output_contract()
        dependencies = self._runtime.container.agent_factory.dependencies(
            deps=deps,
            output_contract=contract,
            toolsets=tuple(toolsets) if toolsets is not None else None,
            command_protocol=command_protocol,
        )
        schema_kwargs: dict[str, Any] = {
            "name": name,
            "command_protocol": CommandProtocol(command_protocol).value,
        }
        if tools is not None:
            schema_kwargs["tools"] = list(tools)
        elif toolsets is not None:
            declared: list[str] = []
            for toolset in toolsets:
                toolset.prepare()
                declared.extend(toolset.tool_names())
            schema_kwargs["tools"] = list(dict.fromkeys(declared))
        role = self._runtime.agent(
            name=name,
            role_schema=RoleSchema(**schema_kwargs),
            dependencies=dependencies,
        )
        return Agent._create(
            driver=role,
            release=lambda: self._runtime.release(role),
            is_open=lambda: self._runtime.state is EngineState.OPEN,
        )

    def _build_role(self, **kwargs: Any) -> Role[Any, Any]:
        role = self._runtime.container.agent_factory.build(
            Role,
            services=self._runtime.services,
            **kwargs,
        )
        role.state.working_dir = self._cwd
        role.state.original_working_dir = self._cwd
        role.state.project_root = find_git_root(self._cwd) or self._cwd
        return role

    async def aclose(self) -> None:
        await self._runtime.aclose()

    async def __aenter__(self) -> "Engine":
        await self._runtime.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return await self._runtime.__aexit__(exc_type, exc, tb)


__all__ = ["Engine"]
