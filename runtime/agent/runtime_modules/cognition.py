"""LLM routing and per-turn cognition component manifest."""

from __future__ import annotations

from collections.abc import Callable

from mote.contracts.config.models import ModelsConfig
from mote.kernel.flow import AgentFlowEngine
from mote.kernel.think.base import BaseThinkEngine
from mote.kernel.think.prompt_builder import ThinkSubsystems
from mote.kernel.think.think_engine import ThinkEngine
from mote.runtime.agent.component_graph import BuildContext, ComponentSpec
from mote.runtime.agent.context_provider import ContextProvider
from mote.runtime.agent.output_engine import OutputEngine
from mote.runtime.durable import ThinkJournal, make_durable_backend
from mote.runtime.models.gateway import DEFAULT_MODEL_NAME, LLMRouter
from mote.runtime.models.routing.catalog import build_route_catalog
from mote.runtime.models.routing.policy import ClassMappedRoutingPolicy, DeterministicRoutingPolicy
from mote.runtime.models.routing.service import RoutingService
from mote.runtime.models.routing.state import RoleRoutingStateStore
from mote.runtime.reporting import ThoughtReporter


def cognition_component_specs() -> list[ComponentSpec]:
    """Return the complete, uniquely owned cognition graph fragment."""
    return [
        ComponentSpec("router", _build_router),
        ComponentSpec("context_provider", lambda ctx: ContextProvider(ctx.role)),
        ComponentSpec("think_engine_factory", _build_think_engine_factory),
        ComponentSpec("think_subsystems_factory", _build_think_subsystems_factory),
        ComponentSpec("flow_engine_factory", _build_flow_engine_factory),
    ]


def _task_route_map(models: ModelsConfig) -> dict[str, str]:
    if not models.endpoints:
        return {task: task for task in models.tasks}
    return {
        **{task: DEFAULT_MODEL_NAME for task in models.tasks},
        **{task: task for task in models.routes.tasks},
    }


def _build_router(ctx) -> LLMRouter:
    role = ctx.role
    agent_config = (
        role.config.router.sub_agent if role.state.parent_session_id is not None else role.config.router.main_agent
    )
    routing_service = None
    if agent_config.strategy is not None:
        gateway = role.context.model_gateway
        if gateway is None:
            raise RuntimeError("semantic routing requires an installed ModelGateway")
        catalog = build_route_catalog(role.config.router, agent_config, gateway)
        if catalog.candidate(agent_config.default_route) is None:
            raise RuntimeError(f"semantic routing default route {agent_config.default_route!r} is unavailable")
        fallback = DeterministicRoutingPolicy(agent_config.default_route)
        if agent_config.strategy == "squilla":
            builder = role.wiring.dependencies.routing_strategy_builders.get("squilla")
            if builder is None:
                raise RuntimeError(
                    "routing strategy 'squilla' is Product-owned and must be injected "
                    "through routing_strategy_builders"
                )
            built = builder()
            policy = ClassMappedRoutingPolicy(built, agent_config.class_routes)
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
            session_fact_sink=ctx.dep("session_fact_committer"),
        )
    router = LLMRouter(
        role.context.model_gateway,
        routing_service=routing_service,
        task_map=_task_route_map(role.config.models),
        session_fact_sink=ctx.dep("session_fact_committer"),
        artifact_resolver=ctx.dep("artifact_resolver"),
    )
    return router


ThinkBuilder = Callable[[BuildContext], BaseThinkEngine]


def _build_flow_engine(ctx: BuildContext, think_engine: BaseThinkEngine) -> AgentFlowEngine:
    role = ctx.role
    executor = ctx.dep("executor")
    lease = role._components.current_output_lease()
    durable_runner = None
    if executor.durable_config.enabled and executor.journal is not None:
        durable_runner = ThinkJournal(make_durable_backend(executor.durable_config, executor.journal))
    return AgentFlowEngine(
        think_engine=think_engine,
        command_channel=ctx.dep("command_channel"),
        executor=executor,
        memory=ctx.dep("context_manager"),
        context_provider=ctx.dep("context_provider"),
        is_active=role._is_active,
        set_active=role._set_active,
        get_bg_pool=role._peek_bg_pool,
        report_think_result=role._report_think_result,
        turn_context_bus=ctx.dep("turn_context_bus"),
        get_cwd=role.get_cwd,
        advance_turn=role._advance_turn,
        durable_runner=durable_runner,
        drain_writes=role.context.disk_writer.drain,
        output_engine=OutputEngine(
            role.output_contract,
            restored_state=role._state_ctl.take_pending_output_restore(),
            run_id=lease.run_id,
            commit_fence=lease,
            fencing_token=lease.fencing_token,
            drain_writes=role.context.disk_writer.drain,
            session_fact_sink=ctx.dep("session_fact_committer"),
        ),
    )


def _build_default_think_engine(ctx: BuildContext) -> BaseThinkEngine:
    return ThinkEngine(
        memory=ctx.dep("context_manager"),
        config=ctx.role.config,
        reporter_factory=ThoughtReporter,
    )


_THINK_BUILDERS: dict[str, ThinkBuilder] = {"default": _build_default_think_engine}


def _build_think_engine_factory(ctx: BuildContext) -> Callable[[], BaseThinkEngine]:
    def make_think_engine() -> BaseThinkEngine:
        builder = _THINK_BUILDERS.get(ctx.role.role_schema.think_kind) or _THINK_BUILDERS["default"]
        return builder(ctx)

    return make_think_engine


def _build_flow_engine_factory(ctx: BuildContext) -> Callable[[], AgentFlowEngine]:
    def make_flow_engine() -> AgentFlowEngine:
        return _build_flow_engine(ctx, ctx.dep("think_engine_factory")())

    return make_flow_engine


def _build_think_subsystems_factory(ctx) -> Callable[[], ThinkSubsystems]:
    def make_think_subsystems() -> ThinkSubsystems:
        role = ctx.role
        return ThinkSubsystems(
            config=role.config,
            model_name=getattr(role.config.models.default, "model", "") or "",
            executor=ctx.dep("executor"),
            skill_manager=ctx.dep("skill_manager"),
            turn_context_bus=ctx.dep("turn_context_bus"),
            command_channel=ctx.dep("command_channel"),
        )

    return make_think_subsystems
