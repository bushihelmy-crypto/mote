#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from mote.contracts.config.llm import LLMConfig, LLMType
from mote.contracts.ports import ModelGateway, ModelOperatorControl, ServiceGateway
from mote.runtime.config.loader import load_config
from mote.runtime.config.schema import Config
from mote.runtime.disk import DiskWriter
from mote.runtime.errors import ProviderNotFoundError
from mote.runtime.lifecycle import LifecyclePhase, LifecycleResource, LifecycleStack, LifecycleState
from mote.runtime.logging import logger
from mote.runtime.maintenance import MaintenanceCoordinator
from mote.runtime.models.clients.base import BaseLLM
from mote.runtime.models.clients.registry import resolve_api_type
from mote.runtime.models.cost import CostTracker, PricingMode
from mote.runtime.models.ratelimit import RateLimitTracker
from mote.runtime.observability.langfuse_integration import LangfuseRuntime
from mote.runtime.resilience import ResourceHealthRegistry

PROVIDER_CLOSE_PHASE = LifecyclePhase.CLOSE_RESOURCES
EXPORTER_CLOSE_PHASE = LifecyclePhase.FLUSH_EXPORTERS
DURABILITY_CLOSE_PHASE = LifecyclePhase.FLUSH_DURABILITY


def _provider_factory_not_configured(config: LLMConfig) -> BaseLLM:
    api_type = resolve_api_type(config)
    raise ProviderNotFoundError(
        "No provider factory was injected into this Runtime Context. "
        "Construct the Context through a Product composition root or pass provider_factory explicitly.",
        api_type=str(api_type),
    )


class Context(BaseModel):
    """LLM build context for Mote.

    Bundles the global :class:`Config` with a :class:`CostTracker` and exposes
    the only capability the router needs: build a :class:`BaseLLM` from the
    default config (``llm``) or an explicit ``LLMConfig``, wiring the right cost
    tracker in each case.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: Config = Field(default_factory=load_config)
    cost_manager: CostTracker = Field(default_factory=CostTracker)
    # Fleet-wide rate-limit quota (provider account state, last-write-wins per
    # endpoint) — the ``/usage`` limit side, counterpart to ``cost_manager``.
    # Shared by every LLM this Context builds; each provider's response hook
    # observes into it. See :mod:`mote.runtime.models.ratelimit`.
    rate_limit_tracker: RateLimitTracker = Field(default_factory=RateLimitTracker)
    health_registry: ResourceHealthRegistry = Field(default_factory=ResourceHealthRegistry)
    disk_writer: DiskWriter = Field(default_factory=DiskWriter)
    maintenance_coordinator: MaintenanceCoordinator = Field(default_factory=MaintenanceCoordinator)
    # Concrete providers are supplied by the Product composition root. The
    # default preserves standalone Runtime use with an explicitly populated
    # registry, while tests and alternate products can inject their own factory.
    provider_factory: Callable[[LLMConfig], BaseLLM] = Field(
        default=_provider_factory_not_configured,
        exclude=True,
    )
    model_gateway: ModelGateway | None = Field(default=None, exclude=True)
    model_operator: ModelOperatorControl | None = Field(default=None, exclude=True)
    service_gateway: ServiceGateway | None = Field(default=None, exclude=True)
    _llms: list[BaseLLM] = PrivateAttr(default_factory=list)
    _lifecycle: LifecycleStack = PrivateAttr(default_factory=LifecycleStack)
    _langfuse: LangfuseRuntime = PrivateAttr(default_factory=LangfuseRuntime)
    _shutdown_started: bool = PrivateAttr(default=False)
    _closed: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _activate_runtime_services(self) -> "Context":
        self.health_registry.set_config(self.config.resilience.to_breaker_config())
        self._langfuse = LangfuseRuntime.from_config(self.config.observability.langfuse)
        self._lifecycle.register_close(
            "observability:langfuse",
            self._langfuse.aclose,
            phase=EXPORTER_CLOSE_PHASE,
        )
        self._lifecycle.register_close(
            "durability:disk-writer",
            self.disk_writer.aclose,
            phase=DURABILITY_CLOSE_PHASE,
        )
        return self

    @property
    def langfuse(self) -> LangfuseRuntime:
        return self._langfuse

    def register_resource(self, resource: LifecycleResource) -> None:
        """Add an Engine-shared resource before shutdown begins."""

        self._lifecycle.register(resource)

    def _select_cost_manager(self, llm_config: LLMConfig) -> CostTracker:
        """Return a CostTracker whose pricing mode matches the config's api_type.

        Self-hosted open models cost nothing (FREE), Fireworks bills by model
        size (FIREWORKS), everything else uses the standard cache-aware table
        and shares the session-wide tracker so per-model totals roll up.

        Keys off the *resolved* api_type (same source the provider client is
        chosen from) so pricing mode and provider can never disagree — e.g. a
        Claude-via-anthropic.com config never bills on the Fireworks table.
        """
        api_type = resolve_api_type(llm_config)
        if api_type == LLMType.FIREWORKS:
            return CostTracker(mode=PricingMode.FIREWORKS)
        elif api_type == LLMType.OPEN_LLM:
            return CostTracker(mode=PricingMode.FREE)
        else:
            return self.cost_manager

    def llm(self) -> BaseLLM:
        """Build a BaseLLM for the default (``config.models.default``) model."""
        default = self.config.models.default
        llm = self._track(self.provider_factory(default))
        if llm.cost_manager is None:
            llm.cost_manager = self._select_cost_manager(default)
        llm.rate_limit_tracker = self.rate_limit_tracker
        return llm

    def llm_with_cost_manager_from_llm_config(self, llm_config: LLMConfig) -> BaseLLM:
        """Build a BaseLLM from a given ``LLMConfig``."""
        llm = self._track(self.provider_factory(llm_config))
        if llm.cost_manager is None:
            llm.cost_manager = self._select_cost_manager(llm_config)
        llm.rate_limit_tracker = self.rate_limit_tracker
        return llm

    def _track(self, llm: BaseLLM) -> BaseLLM:
        if self._shutdown_started:
            raise RuntimeError("Context is closed and cannot create provider clients.")
        if all(existing is not llm for existing in self._llms):
            self._llms.append(llm)
            self._lifecycle.register_close(
                f"provider:{id(llm)}",
                lambda: self._close_llm(llm),
                phase=PROVIDER_CLOSE_PHASE,
            )
        return llm

    async def _close_llm(self, llm: BaseLLM) -> None:
        close = getattr(llm, "aclose", None)
        if close is not None:
            await close()
        self._llms = [existing for existing in self._llms if existing is not llm]

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
