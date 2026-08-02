"""Construction helpers for the Interface-independent Product object graph."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from mote.contracts.config.runtime_client import RuntimeClientActivationSpec
from mote.contracts.ports.task.operations import BackgroundTaskBuildContext, BackgroundTaskService
from mote.orchestration.agents.budget import AgentBudgetCoordinator
from mote.product.agents.background_tasks import AgentBackgroundTasks, build_background_task_pool
from mote.product.agents.deferred_projection import build_deferred_result_projector
from mote.product.composition.agent_factory import build_product_agent
from mote.product.composition.application import Application
from mote.product.composition.clock import build_clock_source
from mote.product.composition.container import ProductContainer
from mote.product.composition.lifecycle import lifecycle_resources
from mote.product.composition.model_application import AtomicApplicationComposition
from mote.product.composition.model_reload import ApplicationReloadCoordinator
from mote.product.composition.model_startup import install_initial_application_composition
from mote.product.composition.service_gateway import builtin_service_gateway
from mote.product.config.bootstrap import ensure_mote_home
from mote.product.config.schema import Config
from mote.product.extensions.sources import ApprovedExtensionSnapshot
from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore, SQLiteUsageLedger
from mote.product.paths import RuntimePaths
from mote.product.workflows.agent_service import AgentWorkflowService
from mote.product.workflows.durability import ProductWorkflowDurability, TrustedWorkflowBlueprint
from mote.product.workflows.temporal_catalog import activate_temporal_effect_plane
from mote.runtime.control.lifecycle import LifecyclePhase, LifecycleResource
from mote.runtime.engine import ClosableAgent, EngineAgentRequest
from mote.runtime.models.clients.context import Context
from mote.runtime.models.composition_context import CurrentRuntimeModelGateway
from mote.runtime.models.failover import LocalModelCallJournal, model_call_journal_root
from mote.runtime.resilience.admission import ResourceAdmissionController
from mote.runtime.resilience.failover.operator import LocalModelOperatorAuditStore, model_operator_audit_path
from mote.runtime.service_gateway import LocalServiceCallJournal, service_call_journal_root
from mote.runtime.services import EngineServices
from mote.runtime.session.workspace.store import SessionWorkspace

AgentT = TypeVar("AgentT", bound=ClosableAgent)


@dataclass(frozen=True, slots=True)
class ApplicationBuildRequest:
    """Validated Product inputs for one Application generation."""

    config: Config
    paths: RuntimePaths
    cwd: Path
    tools: tuple[str, ...] | None = None
    reload_config: Callable[[], Config] | None = None
    extension_approvals: ApprovedExtensionSnapshot = ApprovedExtensionSnapshot()
    workflow_blueprints: tuple[TrustedWorkflowBlueprint, ...] = ()


def prepare_application_sources(paths: RuntimePaths) -> None:
    """Explicitly activate first-run Product configuration scaffolding."""

    ensure_mote_home(paths.user_config_root, package_dir=paths.package_data_root)


async def _settle_failed_activation(
    container: ProductContainer,
    context: Context,
    composition: AtomicApplicationComposition | None,
) -> tuple[BaseException, ...]:
    failures: list[BaseException] = []
    if composition is not None:
        try:
            await composition.aclose()
        except BaseException as error:
            failures.append(error)
    try:
        await context.aclose()
    except BaseException as error:
        failures.append(error)
    try:
        await container.routing_models.aclose()
    except BaseException as error:
        failures.append(error)
    return tuple(failures)


def _build_application_context(config: Config, *, paths: RuntimePaths) -> Context:
    """Purely construct the Runtime context owned by one Product application."""

    context = Context(
        activation=RuntimeClientActivationSpec(
            breaker=config.resilience.to_breaker_config(),
            langfuse=config.observability.langfuse,
        )
    )
    context.model_operator = ResourceAdmissionController(
        breaker_config=config.resilience.to_breaker_config(),
        operator_audit=LocalModelOperatorAuditStore(model_operator_audit_path(paths.workspace_root)),
    )
    return context


async def activate_application_composition(
    config: Config,
    *,
    container: ProductContainer,
    context: Context,
    paths: RuntimePaths,
) -> AtomicApplicationComposition:
    """Activate model/service resources and settle partial activation in reverse."""

    composition = await install_initial_application_composition(
        config,
        providers=container.providers,
        oauth_root=paths.oauth_root,
        cost_tracker=context.cost_manager,
        admission_controller=context.model_operator,
        model_call_journal=LocalModelCallJournal(model_call_journal_root(paths.workspace_root)),
    )
    try:
        application_lease = await composition.acquire()
        try:
            runtime_lease = await application_lease.acquire_runtime()
            try:
                service_gateway = builtin_service_gateway(
                    config.multimodal,
                    config.tools.web_search,
                    model_gateway=CurrentRuntimeModelGateway(),
                    model_profile_gateway=runtime_lease.gateway,
                    media_providers=container.media_providers,
                    search_backends=container.search_backends,
                    admission_controller=context.model_operator,
                    service_call_journal=LocalServiceCallJournal(service_call_journal_root(paths.workspace_root)),
                    activate_reconciliation=True,
                )
                context.service_gateway = service_gateway
                context.register_resource(
                    LifecycleResource(
                        name="hosted-service:reconciler-gateway",
                        phase=LifecyclePhase.STOP_PRODUCERS,
                        close=service_gateway.aclose,
                    )
                )
            finally:
                await runtime_lease.aclose()
        finally:
            await application_lease.aclose()
    except BaseException:
        await composition.aclose()
        raise
    return composition


async def activate_application(request: ApplicationBuildRequest) -> Application[ClosableAgent]:
    """Construct and activate the sole Product Application object graph."""

    workflow_durability = ProductWorkflowDurability(request.paths.workspace_root / ".runtime" / "workflows")
    for blueprint in request.workflow_blueprints:
        workflow_durability.register_trusted_blueprint(
            blueprint.blueprint_id,
            blueprint.blueprint_version,
            blueprint.factory,
        )
    durable_config = request.config.tools.durable
    if durable_config.enabled and durable_config.backend == "temporal":
        workflow_durability.attach_temporal_effect_plane(
            activate_temporal_effect_plane(
                durable_config.temporal,
                workspace=SessionWorkspace(request.paths.workspace_root),
                dispatch=workflow_durability.dispatch_temporal_effect,
            )
        )

    def background_task_builder(
        context: BackgroundTaskBuildContext,
    ) -> BackgroundTaskService:
        return build_background_task_pool(context)

    def deferred_result_projector(service: BackgroundTaskService, artifact_publisher, workflow_nodes):
        if not isinstance(service, AgentBackgroundTasks):
            raise TypeError("Workflow composition requires AgentBackgroundTasks")
        return build_deferred_result_projector(
            service,
            artifact_publisher,
            workflow_nodes,
            AgentWorkflowService(workflow_durability, service, artifact_publisher, workflow_nodes),
        )

    container = ProductContainer.standard(
        request.config,
        cwd=request.cwd,
        paths=request.paths,
        extension_approvals=request.extension_approvals,
        background_task_pool_builder=background_task_builder,
        deferred_result_projector_factory=deferred_result_projector,
    )
    context = _build_application_context(request.config, paths=request.paths)
    composition: AtomicApplicationComposition | None = None
    try:
        # Explicitly activate the approved optional routing backend here. Merely
        # importing or constructing Product composition never resolves it.
        await container.routing_models.prewarm()
        composition = await activate_application_composition(
            request.config,
            container=container,
            context=context,
            paths=request.paths,
        )
        assert composition is not None
        agent_budget_authority = SQLiteAttemptReceiptStore(
            request.paths.workspace_root / ".runtime" / "agent-governance.sqlite3"
        )
        await agent_budget_authority.initialize()
        await agent_budget_authority.reconcile_incomplete()
        agent_usage_ledger = SQLiteUsageLedger(
            agent_budget_authority,
            clock_source=build_clock_source(),
        )
        agent_budget = AgentBudgetCoordinator(
            agent_usage_ledger,
            configurator=agent_usage_ledger,
        )
        await workflow_durability.start()
        services = EngineServices(
            context=context,
            resources=(
                *lifecycle_resources(container.routing_models),
                LifecycleResource(
                    "application-composition",
                    LifecyclePhase.CLOSE_RESOURCES,
                    composition.aclose,
                ),
                LifecycleResource(
                    "workflow-durability",
                    LifecyclePhase.STOP_PRODUCERS,
                    workflow_durability.aclose,
                ),
            ),
            application_composition=composition,
            agent_budget=agent_budget,
            workflow_governance=workflow_durability,
            application_reloader=(
                ApplicationReloadCoordinator(
                    composition=composition,
                    load_config=request.reload_config,
                    providers=container.providers,
                    oauth_root=request.paths.oauth_root,
                    cost_tracker=context.cost_manager,
                    admission_controller=context.model_operator,
                    model_call_journal=LocalModelCallJournal(model_call_journal_root(request.paths.workspace_root)),
                )
                if request.reload_config is not None
                else None
            ),
        )

        def agent_builder(agent_request: EngineAgentRequest) -> ClosableAgent:
            return build_product_agent(
                services=services,
                agent_factory=container.agent_factory,
                agent_catalog=container.agents,
                paths=request.paths,
                source_policy=container.extension_sources,
                name=agent_request.name,
                tools=request.tools,
                cwd=request.cwd,
                agent_type=agent_request.agent_type,
                session_id=agent_request.session_id,
            )

        return _build_application(container=container, services=services, agent_factory=agent_builder)
    except BaseException as activation_error:
        workflow_cleanup_failures: tuple[BaseException, ...] = ()
        try:
            await workflow_durability.aclose()
        except BaseException as workflow_cleanup_error:
            workflow_cleanup_failures = (workflow_cleanup_error,)
        cleanup_failures = (
            *workflow_cleanup_failures,
            *(await _settle_failed_activation(container, context, composition)),
        )
        if cleanup_failures:
            raise BaseExceptionGroup(
                "Product application activation and reverse settlement failed",
                (activation_error, *cleanup_failures),
            ) from None
        raise


def _build_application(
    *,
    container: ProductContainer,
    services: EngineServices,
    agent_factory: Callable[[EngineAgentRequest], AgentT],
) -> Application[AgentT]:
    return Application(
        container=container,
        services=services,
        agent_factory=agent_factory,
    )


__all__ = [
    "ApplicationBuildRequest",
    "activate_application",
    "activate_application_composition",
    "prepare_application_sources",
]
