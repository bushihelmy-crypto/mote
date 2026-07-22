"""LLM routing and per-turn cognition component manifest."""

from __future__ import annotations

from collections.abc import Callable

from mote.common.base import BaseThinkEngine
from mote.loop import BaseLoop, ReActLoop
from mote.loop.durable import DurableRunner, make_durable_backend
from mote.roles.component_graph import BuildContext, ComponentSpec
from mote.roles.context_provider import ContextProvider
from mote.router.ml.engine import shared_engine
from mote.router.router import LLMRouter
from mote.router.squilla import SquillaStrategy
from mote.router.strategy import RuleBasedStrategy
from mote.think.prompt_builder import ThinkSubsystems
from mote.think.think_engine import ThinkEngine


def cognition_component_specs() -> list[ComponentSpec]:
    """Return the complete, uniquely owned cognition graph fragment."""
    return [
        ComponentSpec("router", _build_router),
        ComponentSpec("context_provider", lambda ctx: ContextProvider(ctx.role)),
        ComponentSpec("think_engine_factory", _build_think_engine_factory),
        ComponentSpec("think_subsystems_factory", _build_think_subsystems_factory),
        ComponentSpec("loop_factory", _build_loop_factory),
    ]


def _build_router(ctx) -> LLMRouter:
    role = ctx.role
    router = LLMRouter(role.context)
    agent_config = (
        role.config.router.sub_agent if role.state.parent_session_id is not None else role.config.router.main_agent
    )
    if agent_config.strategy == "squilla":
        router.set_strategy(SquillaStrategy(engine=shared_engine()))
    elif agent_config.strategy == "rule":
        router.set_strategy(RuleBasedStrategy())
    router.routing_enabled = agent_config.strategy is not None
    return router


LoopBuilder = Callable[[BuildContext, BaseThinkEngine], BaseLoop]
ThinkBuilder = Callable[[BuildContext], BaseThinkEngine]


def _build_react_loop(ctx: BuildContext, think_engine: BaseThinkEngine) -> ReActLoop:
    role = ctx.role
    executor = ctx.dep("executor")
    durable_runner = None
    if executor.durable_config.enabled and executor.journal is not None:
        durable_runner = DurableRunner(make_durable_backend(executor.durable_config, executor.journal))
    return ReActLoop(
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
    )


_LOOP_BUILDERS: dict[str, LoopBuilder] = {"react": _build_react_loop}


def _build_default_think_engine(ctx: BuildContext) -> BaseThinkEngine:
    return ThinkEngine(memory=ctx.dep("context_manager"), config=ctx.role.config)


_THINK_BUILDERS: dict[str, ThinkBuilder] = {"default": _build_default_think_engine}


def _build_think_engine_factory(ctx: BuildContext) -> Callable[[], BaseThinkEngine]:
    def make_think_engine() -> BaseThinkEngine:
        builder = _THINK_BUILDERS.get(ctx.role.role_schema.think_kind) or _THINK_BUILDERS["default"]
        return builder(ctx)

    return make_think_engine


def _build_loop_factory(ctx: BuildContext) -> Callable[[], BaseLoop]:
    def make_loop() -> BaseLoop:
        builder = _LOOP_BUILDERS.get(ctx.role.role_schema.loop_kind) or _LOOP_BUILDERS["react"]
        return builder(ctx, ctx.dep("think_engine_factory")())

    return make_loop


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
