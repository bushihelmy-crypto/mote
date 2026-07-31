from __future__ import annotations

import asyncio

import pytest

from mote.kernel.output import text_output_contract
from mote.runtime.agent import AgentDependencies, AgentWiring, Role
from mote.runtime.control.lifecycle import LifecyclePhase, LifecycleResource
from mote.runtime.engine import Engine, EngineAgentRequest, EngineState
from mote.runtime.models.clients.context import EXPORTER_CLOSE_PHASE, Context
from mote.runtime.persistence import DiskWriter
from mote.runtime.services import EngineServices


class _Provider:
    def __init__(self, events: list[str]):
        self.events = events
        self.cost_manager = None
        self.rate_limit_tracker = None

    async def aclose(self) -> None:
        self.events.append("provider")


class _Writer(DiskWriter):
    def __init__(self, events: list[str]):
        super().__init__()
        self.events = events

    async def aclose(self) -> None:
        await super().aclose()
        self.events.append("writer")


class _Agent:
    def __init__(self, name: str, events: list[str]):
        self.name = name
        self.events = events

    async def cleanup(self) -> None:
        self.events.append(f"agent:{self.name}")


@pytest.mark.asyncio
async def test_engine_closes_agents_then_providers_then_writer() -> None:
    events: list[str] = []
    context = Context(disk_writer=_Writer(events))
    context.register_resource(
        LifecycleResource("provider:test", LifecyclePhase.CLOSE_RESOURCES, _Provider(events).aclose)
    )
    engine = Engine(
        services=EngineServices(context=context),
        agent_factory=lambda request: _Agent(request.name, events),
    )
    engine.agent(EngineAgentRequest(name="one"))
    engine.agent(EngineAgentRequest(name="two"))

    await engine.aclose()
    await engine.aclose()

    assert events == ["agent:two", "agent:one", "provider", "writer"]
    assert engine.state is EngineState.CLOSED
    with pytest.raises(RuntimeError, match="new Agents are not accepted"):
        engine.agent(EngineAgentRequest(name="late"))


@pytest.mark.asyncio
async def test_engine_shutdown_survives_waiter_cancellation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowAgent:
        async def cleanup(self) -> None:
            entered.set()
            await release.wait()

    context = Context()
    engine = Engine(
        services=EngineServices(context=context),
        agent_factory=lambda _request: SlowAgent(),
    )
    engine.agent(EngineAgentRequest(name="slow"))

    waiter = asyncio.create_task(engine.aclose())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release.set()
    await engine.aclose()
    assert engine.state is EngineState.CLOSED


@pytest.mark.asyncio
async def test_release_drops_agent_ownership_and_prevents_double_cleanup() -> None:
    events: list[str] = []
    context = Context(disk_writer=_Writer(events))
    engine = Engine(
        services=EngineServices(context=context),
        agent_factory=lambda request: _Agent(request.name, events),
    )
    agent = engine.agent(EngineAgentRequest(name="released"))

    await engine.release(agent)
    await engine.release(agent)
    await engine.aclose()

    assert events == ["agent:released", "writer"]
    assert engine._agents == {}


@pytest.mark.asyncio
async def test_engine_retries_failed_agent_before_closing_context() -> None:
    events: list[str] = []

    class RetryAgent:
        def __init__(self) -> None:
            self.attempts = 0

        async def cleanup(self) -> None:
            self.attempts += 1
            events.append(f"agent:{self.attempts}")
            if self.attempts == 1:
                raise RuntimeError("transient close failure")

    context = Context(disk_writer=_Writer(events))
    context.register_resource(
        LifecycleResource("provider:test", LifecyclePhase.CLOSE_RESOURCES, _Provider(events).aclose)
    )
    engine = Engine(
        services=EngineServices(context=context),
        agent_factory=lambda _request: RetryAgent(),
    )
    engine.agent(EngineAgentRequest(name="retry"))

    with pytest.raises(Exception, match="transient close failure"):
        await engine.aclose()
    assert events == ["agent:1"]
    assert engine.state is EngineState.CLOSING

    await engine.aclose()
    assert events == ["agent:1", "agent:2", "provider", "writer"]
    assert engine.state is EngineState.CLOSED


@pytest.mark.asyncio
async def test_context_retries_provider_before_closing_writer() -> None:
    events: list[str] = []

    class RetryProvider(_Provider):
        def __init__(self) -> None:
            super().__init__(events)
            self.attempts = 0

        async def aclose(self) -> None:
            self.attempts += 1
            events.append(f"provider:{self.attempts}")
            if self.attempts == 1:
                raise RuntimeError("provider close failed")

    provider = RetryProvider()
    context = Context(disk_writer=_Writer(events))
    context.register_resource(LifecycleResource("provider:test", LifecyclePhase.CLOSE_RESOURCES, provider.aclose))

    with pytest.raises(RuntimeError, match="provider close failed"):
        await context.aclose()
    assert events == ["provider:1"]

    await context.aclose()
    assert events == ["provider:1", "provider:2", "writer"]


@pytest.mark.asyncio
async def test_context_closes_provider_then_exporter_then_durability() -> None:
    events: list[str] = []
    context = Context(disk_writer=_Writer(events))
    context.register_resource(
        LifecycleResource("provider:test", LifecyclePhase.CLOSE_RESOURCES, _Provider(events).aclose)
    )
    context.register_resource(
        LifecycleResource(
            name="exporter:test",
            phase=EXPORTER_CLOSE_PHASE,
            close=lambda: events.append("exporter"),
        )
    )
    await context.aclose()

    assert events == ["provider", "exporter", "writer"]


@pytest.mark.asyncio
async def test_role_cleanup_retries_failed_owned_context() -> None:
    class RetryContext:
        def __init__(self) -> None:
            self.attempts = 0

        async def aclose(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("owned context close failed")

    context = RetryContext()
    role = Role(wiring=AgentWiring.for_context(context, owned=True))

    with pytest.raises(RuntimeError, match="owned context close failed"):
        await role.cleanup()
    await role.cleanup()

    assert context.attempts == 2
    assert role._cleanup_complete is True


@pytest.mark.asyncio
async def test_owned_role_context_is_closed_with_session() -> None:
    context = Context(provider_factory=lambda _config: _Provider([]))
    role = Role(wiring=AgentWiring.for_context(context, owned=True))

    await role.cleanup()
    await role.cleanup()

    assert context._closed is True


@pytest.mark.asyncio
async def test_isolated_service_ownership_is_reference_counted_across_incarnations() -> None:
    context = Context(provider_factory=lambda _config: _Provider([]))
    parent = AgentWiring.for_context(context, owned=True)
    child = parent.for_incarnation()

    assert child.dependencies is parent.dependencies
    assert child.services is parent.services
    assert child.services_lease is not parent.services_lease

    assert parent.services_lease is not None
    await parent.services_lease.aclose()
    assert context._closed is False

    assert child.services_lease is not None
    await child.services_lease.aclose()
    assert context._closed is True


@pytest.mark.asyncio
async def test_engine_services_reject_direct_close_while_isolated_owner_is_live() -> None:
    context = Context(provider_factory=lambda _config: _Provider([]))
    services = EngineServices(context=context)
    lease = services.acquire()

    with pytest.raises(RuntimeError, match="isolated owner"):
        await services.aclose()
    await lease.aclose()

    assert context._closed is True


def test_agent_wiring_freezes_product_collections() -> None:
    source = {"rule": object}
    wiring = AgentWiring(
        dependencies=AgentDependencies(
            deps=None,
            output_contract=text_output_contract(),
            routing_strategy_builders=source,
            toolsets=[object()],
        )
    )
    source["later"] = object

    assert tuple(wiring.dependencies.routing_strategy_builders) == ("rule",)
    assert isinstance(wiring.dependencies.toolsets, tuple)
    with pytest.raises(TypeError):
        wiring.dependencies.routing_strategy_builders["mutate"] = object
