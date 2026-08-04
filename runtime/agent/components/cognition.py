"""LLM routing and per-turn cognition component manifest."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from mote.contracts.model.checkpoint import ModelCheckpointPolicy
from mote.contracts.ports.agent.composition import RoutingStrategyFactory
from mote.kernel.execution import ExecutionEngine
from mote.kernel.inference.base import BaseInferenceEngine
from mote.kernel.inference.engine import InferenceEngine
from mote.kernel.inference.prompt_builder import InferenceSubsystems
from mote.runtime.agent.component_graph import BuildContext, ComponentKey, ComponentSpec

if TYPE_CHECKING:
    from mote.runtime.agent.component_projection import AgentComponentProjection
    from mote.runtime.agent.role import Role

from mote.runtime.agent.component_keys import (
    ARTIFACT_RESOLVER,
    COMMAND_CHANNEL,
    CONTEXT_MANAGER,
    CONTEXT_PROVIDER,
    EXECUTOR,
    INFERENCE_ENGINE_FACTORY,
    INFERENCE_PORT,
    INFERENCE_SUBSYSTEMS_FACTORY,
    ROUTER,
    SESSION_FACT_COMMITTER,
    SKILL_MANAGER,
    TOOL_SNAPSHOT_MANAGER,
    TURN_CONTEXT_BUS,
)
from mote.runtime.agent.components.context_provider import ContextProvider
from mote.runtime.durable.inference_checkpoint import InferenceCheckpoint
from mote.runtime.events.context import observe_event_sync
from mote.runtime.models.gateway import LLMRouter
from mote.runtime.models.inference_port import RuntimeModelInferencePort
from mote.runtime.models.output_snapshots import bind_output_snapshot_accumulator
from mote.runtime.models.routing.catalog import build_route_catalog
from mote.runtime.models.routing.policy import ClassMappedRoutingPolicy, DeterministicRoutingPolicy
from mote.runtime.models.routing.service import RoutingService
from mote.runtime.models.routing.state import RoleRoutingStateStore
from mote.runtime.models.session_projection import ModelSessionProjectionStore
from mote.runtime.output.engine import OutputEngine
from mote.runtime.persistence.execution_transaction import RuntimeExecutionTransaction
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.telemetry.reporting import ThoughtReporter
from mote.runtime.tools.snapshots import RuntimeToolSnapshotManager

OutputT = TypeVar("OutputT")


@dataclass(frozen=True, slots=True)
class CognitionComponentInputs:
    routing_strategy_factory: RoutingStrategyFactory | None = None
    component_projection: AgentComponentProjection | None = None
    model_checkpoint_policy: ModelCheckpointPolicy | None = None


def cognition_component_specs(
    execution_engine_factory_key: ComponentKey[Callable[[], ExecutionEngine[OutputT]]],
    *,
    inputs: CognitionComponentInputs = CognitionComponentInputs(),
) -> list[ComponentSpec]:
    """Return the complete, uniquely owned cognition graph fragment."""
    return [
        ComponentSpec(ROUTER, lambda ctx: _build_router(ctx, inputs.routing_strategy_factory)),
        ComponentSpec(
            INFERENCE_PORT,
            lambda ctx: RuntimeModelInferencePort(router=ctx.dep(ROUTER), role=ctx.role),
        ),
        ComponentSpec(
            TOOL_SNAPSHOT_MANAGER,
            _build_tool_snapshot_manager,
        ),
        ComponentSpec(
            CONTEXT_PROVIDER,
            lambda ctx: ContextProvider(
                ctx.role,
                ctx.dep(INFERENCE_PORT),
                ctx.dep(TOOL_SNAPSHOT_MANAGER),
            ),
        ),
        ComponentSpec(INFERENCE_ENGINE_FACTORY, _build_think_engine_factory),
        ComponentSpec(INFERENCE_SUBSYSTEMS_FACTORY, _build_think_subsystems_factory),
        ComponentSpec(
            execution_engine_factory_key,
            lambda ctx: _build_flow_engine_factory(
                ctx,
                inputs.component_projection,
                inputs.model_checkpoint_policy,
            ),
        ),
    ]


def _build_tool_snapshot_manager(ctx: BuildContext["Role", object]) -> RuntimeToolSnapshotManager:
    return RuntimeToolSnapshotManager(
        ctx.dep(EXECUTOR),
        composition_generation_id=(ctx.role._components.current_application_generation_id()),
    )


def _build_router(ctx, routing_strategy_factory: RoutingStrategyFactory | None) -> LLMRouter:
    role = ctx.role
    model_gateway = role._components.current_runtime_composition().gateway
    agent_config = (
        role.config.router.sub_agent if role.state.parent_session_id is not None else role.config.router.main_agent
    )
    routing_service = None
    if agent_config.strategy is not None:
        gateway = model_gateway
        if gateway is None:
            raise RuntimeError("semantic routing requires an installed ModelGateway")
        catalog = build_route_catalog(role.config.router, agent_config, gateway)
        if catalog.candidate(catalog.default_route_id) is None:
            raise RuntimeError(f"semantic routing default route {agent_config.default_route!r} is unavailable")
        fallback = DeterministicRoutingPolicy(catalog.default_route_id)
        if agent_config.strategy == "squilla":
            built = routing_strategy_factory.build("squilla") if routing_strategy_factory is not None else None
            if built is None:
                raise RuntimeError(
                    "routing strategy 'squilla' is Product-owned and must be injected " "through RoutingStrategyFactory"
                )
            policy = ClassMappedRoutingPolicy(built, dict(catalog.class_routes))
        else:
            policy = fallback
        state_store = RoleRoutingStateStore(
            lambda: role.state.routing,
            lambda state: setattr(role.state, "routing", state),
        )
        routing_service = RoutingService(
            catalog,
            policy,
            fallback,
            state_store,
            deadline_ms=agent_config.deadline_ms,
            session_fact_sink=ctx.dep(SESSION_FACT_COMMITTER),
            authority_revision=lambda: catalog.revision,
        )
    router = LLMRouter(
        model_gateway,
        routing_service=routing_service,
        default_route=role.role_schema.model_route,
        session_fact_sink=ctx.dep(SESSION_FACT_COMMITTER),
        artifact_resolver=ctx.dep(ARTIFACT_RESOLVER),
    )
    return router


ThinkBuilder = Callable[[BuildContext], BaseInferenceEngine]


def _build_flow_engine(
    ctx: BuildContext,
    inference_engine: BaseInferenceEngine,
    component_projection: AgentComponentProjection | None,
    model_checkpoint_policy: ModelCheckpointPolicy | None,
) -> ExecutionEngine[
    OutputT
]:  # pyright: ignore[reportInvalidTypeVarUse] -- OutputT is fixed by the typed ComponentKey factory consumer.
    role = ctx.role
    executor = ctx.dep(EXECUTOR)
    lease = role._components.current_output_lease()
    model_gateway = ctx.dep(ROUTER).gateway
    if component_projection is None or model_gateway is None or model_checkpoint_policy is None:
        raise RuntimeError("Inference checkpoint requires Product workspace and ModelCall composition")
    checkpoint = InferenceCheckpoint(
        projections=ModelSessionProjectionStore(
            role.state.session_id,
            SessionWorkspace(component_projection.session_workspace_root()),
            model_checkpoint_policy,
        ),
        model_calls=model_gateway,
        inference_engine=inference_engine,
        artifact_resolver=ctx.dep(ARTIFACT_RESOLVER),
    )
    output_engine = OutputEngine(
        role.output_contract,
        restored_state=role._state_ctl.take_pending_output_restore(),
        run_id=lease.run_id,
        commit_fence=lease,
        fencing_token=lease.fencing_token,
        drain_writes=role._context.disk_writer.drain,
        session_fact_sink=ctx.dep(SESSION_FACT_COMMITTER),
    )
    execution_transaction = RuntimeExecutionTransaction(
        run_id=lease.run_id,
        fencing_token=lease.fencing_token,
        memory=ctx.dep(CONTEXT_MANAGER),
        output_engine=output_engine,
        inference_checkpoint=checkpoint,
        drain_writes=role._context.disk_writer.drain,
    )
    return ExecutionEngine(
        inference_engine=inference_engine,
        command_channel=ctx.dep(COMMAND_CHANNEL),
        executor=executor,
        tool_execution_port=ctx.dep(TOOL_SNAPSHOT_MANAGER),
        memory=ctx.dep(CONTEXT_MANAGER),
        context_provider=ctx.dep(CONTEXT_PROVIDER),
        is_active=role._is_active,
        set_active=role._set_active,
        get_bg_pool=role._peek_bg_pool,
        report_inference_result=role._report_think_result,
        inference_checkpoint=checkpoint,
        execution_transaction=execution_transaction,
        turn_context_bus=ctx.dep(TURN_CONTEXT_BUS),
        get_cwd=role.get_cwd,
        advance_turn=role._advance_turn,
        output_engine=output_engine,
    )


def _build_default_think_engine(ctx: BuildContext) -> BaseInferenceEngine:
    return InferenceEngine(
        memory=ctx.dep(CONTEXT_MANAGER),
        config=ctx.role.config,
        inference_port=ctx.dep(INFERENCE_PORT),
        snapshot_scope=bind_output_snapshot_accumulator,
        output_observer=observe_event_sync,
        reporter_factory=ThoughtReporter,
    )


_THINK_BUILDERS: dict[str, ThinkBuilder] = {"default": _build_default_think_engine}


def _build_think_engine_factory(ctx: BuildContext) -> Callable[[], BaseInferenceEngine]:
    def make_think_engine() -> BaseInferenceEngine:
        builder = _THINK_BUILDERS.get(ctx.role.role_schema.inference_kind) or _THINK_BUILDERS["default"]
        return builder(ctx)

    return make_think_engine


def _build_flow_engine_factory(
    ctx: BuildContext,
    component_projection: AgentComponentProjection | None,
    model_checkpoint_policy: ModelCheckpointPolicy | None,
) -> Callable[
    [], ExecutionEngine[OutputT]
]:  # pyright: ignore[reportInvalidTypeVarUse] -- OutputT is preserved by the registered ComponentKey.
    def make_flow_engine() -> ExecutionEngine[OutputT]:
        return _build_flow_engine(
            ctx,
            ctx.dep(INFERENCE_ENGINE_FACTORY)(),
            component_projection,
            model_checkpoint_policy,
        )

    return make_flow_engine


def _build_think_subsystems_factory(ctx) -> Callable[[], InferenceSubsystems]:
    def make_think_subsystems() -> InferenceSubsystems:
        role = ctx.role
        response_language = role._components.current_runtime_role_config().response_language
        return InferenceSubsystems(
            config=role.config,
            model_name=role.default_model_name or "",
            response_language=response_language,
            executor=ctx.dep(EXECUTOR),
            turn_context_bus=ctx.dep(TURN_CONTEXT_BUS),
            command_channel=ctx.dep(COMMAND_CHANNEL),
        )

    return make_think_subsystems
