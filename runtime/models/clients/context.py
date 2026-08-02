#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from mote.contracts.config.runtime_client import RuntimeClientActivationSpec
from mote.contracts.ports.model.operator import ModelOperatorControl
from mote.contracts.ports.service.gateway import ServiceGateway
from mote.runtime.control.lifecycle import LifecyclePhase, LifecycleResource, LifecycleStack, LifecycleState
from mote.runtime.models.cost import CostTracker
from mote.runtime.models.ratelimit import RateLimitTracker
from mote.runtime.persistence import DiskWriter
from mote.runtime.resilience import ResourceHealthRegistry
from mote.runtime.telemetry.logging import logger
from mote.runtime.telemetry.observability.langfuse_integration import LangfuseRuntime

EXPORTER_CLOSE_PHASE = LifecyclePhase.FLUSH_EXPORTERS
DURABILITY_CLOSE_PHASE = LifecyclePhase.FLUSH_DURABILITY


class Context(BaseModel):
    """LLM build context for Mote.

    Bundles the global :class:`Config` with a :class:`CostTracker` and exposes
    the only capability the router needs: build a :class:`BaseLLM` from the
    default config (``llm``) or an explicit ``LLMConfig``, wiring the right cost
    tracker in each case.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    activation: RuntimeClientActivationSpec = Field(default_factory=RuntimeClientActivationSpec)
    cost_manager: CostTracker = Field(default_factory=CostTracker)
    # Fleet-wide rate-limit quota (provider account state, last-write-wins per
    # endpoint) — the ``/usage`` limit side, counterpart to ``cost_manager``.
    # Shared by every LLM this Context builds; each provider's response hook
    # observes into it. See :mod:`mote.runtime.models.ratelimit`.
    rate_limit_tracker: RateLimitTracker = Field(default_factory=RateLimitTracker)
    health_registry: ResourceHealthRegistry = Field(default_factory=ResourceHealthRegistry)
    disk_writer: DiskWriter = Field(default_factory=DiskWriter)
    model_operator: ModelOperatorControl | None = Field(default=None, exclude=True)
    service_gateway: ServiceGateway | None = Field(default=None, exclude=True)
    _lifecycle: LifecycleStack = PrivateAttr(default_factory=LifecycleStack)
    _langfuse: LangfuseRuntime = PrivateAttr(default_factory=LangfuseRuntime)
    _shutdown_started: bool = PrivateAttr(default=False)
    _closed: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _activate_runtime_services(self) -> "Context":
        self._lifecycle.register_close(
            "durability:disk-writer",
            self.disk_writer.aclose,
            phase=DURABILITY_CLOSE_PHASE,
        )
        self.health_registry.set_config(self.activation.breaker)
        self._langfuse = LangfuseRuntime.from_config(self.activation.langfuse)
        self._lifecycle.register_close(
            "observability:langfuse",
            self._langfuse.aclose,
            phase=EXPORTER_CLOSE_PHASE,
        )
        return self

    @property
    def langfuse(self) -> LangfuseRuntime:
        return self._langfuse

    def register_resource(self, resource: LifecycleResource) -> None:
        """Add an Engine-shared resource before shutdown begins."""

        self._lifecycle.register(resource)

    async def __aenter__(self) -> "Context":
        if self._shutdown_started:
            raise RuntimeError("Context cannot be re-entered after it is closed.")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            await self.aclose()
        else:
            try:
                await self.aclose()
            except Exception as close_exc:  # noqa: BLE001 — preserve body failure
                logger.warning(f"Context shutdown failed while handling {type(exc).__name__}: {close_exc}")
        return False

    async def aclose(self) -> None:
        """Close provider clients, then drain and stop the durable writer."""

        self._shutdown_started = True
        await self._lifecycle.aclose()
        self._closed = self._lifecycle.state is LifecycleState.CLOSED
