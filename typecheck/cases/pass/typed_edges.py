"""Positive static contracts for output, toolset, and telemetry edges."""

from __future__ import annotations

from typing import Protocol, TypeGuard

from mote.contracts.ports.events.telemetry import TelemetryHandler
from mote.kernel.output import text_output_contract
from mote.product.presentation.consumer_protocol import Consumer
from mote.product.presentation.events.capabilities import Capabilities
from mote.product.presentation.events.events import ViewEvent
from mote.product.presentation.input_events import PresentationInputEvent
from mote.product.presentation.projection.projector import ViewProjector
from mote.product.presentation.projection.protocol import Projector
from mote.runtime.agent import AgentDependencies
from mote.runtime.events.telemetry import TypedTelemetryBinding
from mote.runtime.tools.provider import NativeToolset


class CommonDeps(Protocol):
    cwd: str


class CodingDeps(CommonDeps, Protocol):
    language: str


class ConcreteCodingDeps:
    cwd = "/workspace"
    language = "python"


common_tools: NativeToolset[CommonDeps] = NativeToolset("common", ())
coding_deps: CodingDeps = ConcreteCodingDeps()
coding_dependencies: AgentDependencies[CodingDeps, str]
coding_dependencies = AgentDependencies(
    deps=coding_deps,
    output_contract=text_output_contract(),
    toolsets=(common_tools,),
)


class EventA:
    pass


def is_event_a(event: object) -> TypeGuard[EventA]:
    return isinstance(event, EventA)


class HandlerA:
    async def handle(self, event: EventA) -> None:
        del event


handler_a: TelemetryHandler[EventA] = HandlerA()
typed_binding: TypedTelemetryBinding[EventA]
presentation_projector: Projector[PresentationInputEvent, ViewEvent] = ViewProjector()


class ViewConsumer:
    capabilities = Capabilities()

    async def handle(self, ev: ViewEvent) -> None:
        del ev

    async def aclose(self) -> None:
        pass


view_consumer: Consumer[ViewEvent] = ViewConsumer()
