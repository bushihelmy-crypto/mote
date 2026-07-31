"""Public lifecycle and composition facade."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar, overload

from mote.agent import Agent
from mote.contracts.tool import CommandProtocol
from mote.kernel.output import OutputContract, text_output_contract
from mote.model import Model
from mote.product.composition.application import Application
from mote.product.composition.container import ProductContainer
from mote.product.composition.model_reload import ApplicationReloadCoordinator
from mote.product.composition.model_startup import install_initial_application_composition
from mote.product.composition.service_gateway import builtin_service_gateway
from mote.product.config.loader import load_config
from mote.product.paths import default_runtime_paths
from mote.runtime.agent import Role, RoleSchema
from mote.runtime.control.lifecycle import LifecyclePhase, LifecycleResource
from mote.runtime.engine import EngineState
from mote.runtime.models.clients.context import Context
from mote.runtime.models.composition_context import CurrentRuntimeModelGateway
from mote.runtime.models.failover import LocalModelCallJournal, model_call_journal_root
from mote.runtime.resilience.admission import ResourceAdmissionController
from mote.runtime.service_gateway import LocalServiceCallJournal, service_call_journal_root
from mote.runtime.services import EngineServices
from mote.runtime.tools.provider import AnyToolset
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
        paths = default_runtime_paths()
        config = load_config(
            root,
            profile=profile,
            programmatic=selection.config_overlay() if selection is not None else None,
            user_config_root=paths.user_config_root,
            source_root=paths.user_config_root,
        )
        container = ProductContainer.standard(config, cwd=root, paths=paths)
        context = Context(config=config)
        admission = ResourceAdmissionController(breaker_config=config.resilience.to_breaker_config())
        context.model_operator = admission
        services = EngineServices(
            context=context,
            resources=container.lifecycle_resources(),
        )
        self._cwd = str(root)
        self._startup_config = config
        self._startup_paths = paths
        self._active_model: Model | None = None
        self._reload_config = lambda: load_config(
            root,
            profile=profile,
            programmatic=selection.config_overlay() if selection is not None else None,
            user_config_root=paths.user_config_root,
            source_root=paths.user_config_root,
        )
        self._started = False
        self._runtime: Application[Role[Any, Any]] = Application(
            container=container,
            services=services,
            agent_factory=self._build_role,
        )

    @property
    def model(self) -> Model:
        if self._active_model is None:
            raise RuntimeError("Engine model metadata is unavailable before startup")
        return self._active_model

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

        if not self._started:
            raise RuntimeError("Engine must be entered before creating an Agent")

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
        if not self._started:
            services = self._runtime.services
            composition = await install_initial_application_composition(
                self._startup_config,
                providers=self._runtime.container.providers,
                oauth_root=self._startup_paths.oauth_root,
                cost_tracker=services.context.cost_manager,
                admission_controller=services.context.model_operator,
                model_call_journal=LocalModelCallJournal(model_call_journal_root(self._startup_paths.workspace_root)),
            )
            services.application_composition = composition
            services.application_reloader = ApplicationReloadCoordinator(
                composition=composition,
                load_config=self._reload_config,
                providers=self._runtime.container.providers,
                oauth_root=self._startup_paths.oauth_root,
                cost_tracker=services.context.cost_manager,
                admission_controller=services.context.model_operator,
                model_call_journal=LocalModelCallJournal(model_call_journal_root(self._startup_paths.workspace_root)),
            )
            application_lease = await composition.acquire()
            try:
                runtime_lease = await application_lease.acquire_runtime()
                try:
                    self._active_model = Model(
                        name=runtime_lease.default_model.model,
                        provider=runtime_lease.default_model.provider,
                    )
                    services.context.service_gateway = builtin_service_gateway(
                        self._startup_config.multimodal,
                        self._startup_config.tools.web_search,
                        model_gateway=CurrentRuntimeModelGateway(),
                        model_profile_gateway=runtime_lease.gateway,
                        media_providers=self._runtime.container.media_providers,
                        search_backends=self._runtime.container.search_backends,
                        admission_controller=services.context.model_operator,
                        service_call_journal=LocalServiceCallJournal(
                            service_call_journal_root(self._startup_paths.workspace_root)
                        ),
                    )
                finally:
                    await runtime_lease.aclose()
            finally:
                await application_lease.aclose()
            services.register_resource(
                LifecycleResource(
                    "application-composition",
                    LifecyclePhase.CLOSE_RESOURCES,
                    composition.aclose,
                )
            )
            self._started = True
        await self._runtime.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return await self._runtime.__aexit__(exc_type, exc, tb)


__all__ = ["Engine"]
