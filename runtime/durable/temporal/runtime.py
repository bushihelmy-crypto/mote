"""Activated Temporal activity runtime backed by Workflow effect facts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from mote.contracts.config.tool import TemporalConfig
from mote.runtime.durable.temporal._activities import StepActivities, TemporalActivityCatalog


class TemporalActivityRuntime:
    def __init__(self, config: TemporalConfig, catalog: TemporalActivityCatalog) -> None:
        self._config = config
        catalog.freeze()
        self._activities = StepActivities(catalog)

    @property
    def config(self) -> TemporalConfig:
        return self._config

    @property
    def activities(self) -> tuple[Callable[..., Awaitable[str]], ...]:
        return (cast(Callable[..., Awaitable[str]], self._activities.run_step_activity),)


__all__ = ["TemporalActivityRuntime"]
