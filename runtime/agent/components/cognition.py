"""LLM routing and per-turn cognition component manifest."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from mote.kernel.execution import ExecutionEngine
from mote.kernel.inference.base import BaseInferenceEngine
from mote.kernel.inference.engine import InferenceEngine
from mote.kernel.inference.prompt_builder import InferenceSubsystems
from mote.runtime.agent.component_graph import BuildContext, ComponentKey, ComponentSpec
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
from mote.runtime.durable import InferenceJournal, make_durable_backend
from mote.runtime.durable.inference_checkpoint import InferenceCheckpoint
from mote.runtime.models.gateway import LLMRouter
from mote.runtime.models.inference_port import RuntimeModelInferencePort
from mote.runtime.models.output_snapshots import bind_output_snapshot_accumulator
from mote.runtime.models.routing.catalog import build_route_catalog
from mote.runtime.models.routing.policy import ClassMappedRoutingPolicy, DeterministicRoutingPolicy
from mote.runtime.models.routing.service import RoutingService
from mote.runtime.models.routing.state import RoleRoutingStateStore
from mote.runtime.output.engine import OutputEngine
from mote.runtime.persistence.execution_transaction import RuntimeExecutionTransaction
from mote.runtime.telemetry.reporting import ThoughtReporter
from mote.runtime.tools.snapshots import RuntimeToolSnapshotManager

OutputT = TypeVar("OutputT")


def cognition_component_specs(
    execution_engine_factory_key: ComponentKey[Callable[[], ExecutionEngine[OutputT]]],
) -> list[ComponentSpec]:
    """Return the complete, uniquely owned cognition graph fragment."""
    return [
        ComponentSpec(ROUTER, _build_router),
        ComponentSpec(
            INFERENCE_PORT,
            lambda ctx: RuntimeModelInferencePort(router=ctx.dep(ROUTER), role=ctx.role),
        ),
        ComponentSpec(
            TOOL_SNAPSHOT_MANAGER,
            lambda ctx: RuntimeToolSnapshotManager(ctx.dep(EXECUTOR)),
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
        ComponentSpec(execution_engine_factory_key, _build_flow_engine_factory),
    ]


def _build_router(ctx) -> LLMRouter:
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
            builder = role.wiring.dependencies.routing_strategy_builders.get("squilla")
            if builder is None:
                raise RuntimeError(
                    "routing strategy 'squilla' is Product-owned and must be injected "
                    "through routing_strategy_builders"
                )
            built = builder()
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


def _build_flow_engine(ctx: BuildContext, inference_engine: BaseInferenceEngine) -> ExecutionEngine[OutputT]:
    role = ctx.role
    executor = ctx.dep(EXECUTOR)
    lease = role._components.current_output_lease()
    durable_runner = None
    if executor.durable_config.enabled and executor.journal is not None:
        durable_runner = InferenceJournal(make_durable_backend(executor.durable_config, executor.journal))
    checkpoint = InferenceCheckpoint(
        journal_runner=durable_runner,
        memory=ctx.dep(CONTEXT_MANAGER),
        inference_engine=inference_engine,
    )
    output_engine = OutputEngine(
        role.output_contract,
        restored_state=role._state_ctl.take_pending_output_restore(),
        run_id=lease.run_id,
        commit_fence=lease,
        fencing_token=lease.fencing_token,
        drain_writes=role.context.disk_writer.drain,
        session_fact_sink=ctx.dep(SESSION_FACT_COMMITTER),
    )
    execution_transaction = RuntimeExecutionTransaction(
        run_id=lease.run_id,
        fencing_token=lease.fencing_token,
        memory=ctx.dep(CONTEXT_MANAGER),
        output_engine=output_engine,
        inference_checkpoint=checkpoint,
        drain_writes=role.context.disk_writer.drain,
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
) -> Callable[[], ExecutionEngine[OutputT]]:
    def make_flow_engine() -> ExecutionEngine[OutputT]:
        return _build_flow_engine(ctx, ctx.dep(INFERENCE_ENGINE_FACTORY)())

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
            skill_manager=ctx.dep(SKILL_MANAGER),
            turn_context_bus=ctx.dep(TURN_CONTEXT_BUS),
            command_channel=ctx.dep(COMMAND_CHANNEL),
        )

    return make_think_subsystems
