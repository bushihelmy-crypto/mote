"""Narrow Product-provided capabilities used to compose one Runtime Agent."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.ports.model.routing import RoutingPolicy


class RoutingStrategyFactory(Protocol):
    def build(self, name: str) -> RoutingPolicy | None: ...


__all__ = ["RoutingStrategyFactory"]
