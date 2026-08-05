"""Minimal durable output publication service surface."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.output.publication import OutputPublicationReceipt, OutputPublicationRequest
from mote.contracts.ports.agent.routing import AgentRoutingPort


class OutputPublisher(Protocol):
    async def accept(self, request: OutputPublicationRequest) -> OutputPublicationReceipt: ...

    async def reconcile_once(self) -> bool: ...


class OutputPublisherFactory(Protocol):
    def build(self, session_id: str, routing: AgentRoutingPort | None) -> OutputPublisher: ...


__all__ = ["OutputPublisher", "OutputPublisherFactory"]
