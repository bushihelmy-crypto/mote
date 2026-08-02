"""Stable typed Temporal activity catalog; no process-local closure bridge."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict
from temporalio import activity

from mote.contracts.runtime.operation_ownership import EffectCapability

RUN_STEP_ACTIVITY = "mote__run_typed_activity_v1"
ActivityHandler = Callable[["StepInput"], Awaitable[str]]


class StepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "mote.temporal-activity-command/v1"
    step_id: str
    handler_id: str
    payload: str
    kind: str
    effect_id: str
    effect_capability: EffectCapability


class TemporalActivityCatalog:
    """Product-activation catalog keyed by stable implementation identity."""

    def __init__(self) -> None:
        self._handlers: dict[str, ActivityHandler] = {}
        self._frozen = False

    def register(self, handler_id: str, handler: ActivityHandler) -> None:
        if self._frozen:
            raise RuntimeError("Temporal activity catalog is already activated")
        if type(handler_id) is not str or not handler_id:
            raise ValueError("Temporal activity handler identity is required")
        if handler_id in self._handlers:
            raise ValueError(f"duplicate Temporal activity handler {handler_id!r}")
        self._handlers[handler_id] = handler

    def freeze(self) -> None:
        self._frozen = True

    def resolve(self, handler_id: str) -> ActivityHandler:
        if not self._frozen:
            raise RuntimeError("Temporal activity catalog is not activated")
        try:
            return self._handlers[handler_id]
        except KeyError as exc:
            raise RuntimeError(f"unknown Temporal activity handler {handler_id!r}") from exc


class StepActivities:
    def __init__(self, catalog: TemporalActivityCatalog) -> None:
        self._catalog = catalog

        async def run_step_activity(command: StepInput) -> str:
            return await self.run_step(command)

        self.run_step_activity = activity.defn(name=RUN_STEP_ACTIVITY)(run_step_activity)

    async def run_step(self, command: StepInput) -> str:
        if command.schema_version != "mote.temporal-activity-command/v1":
            raise ValueError("unsupported Temporal activity command schema")
        return await self._catalog.resolve(command.handler_id)(command)


__all__ = [
    "ActivityHandler",
    "RUN_STEP_ACTIVITY",
    "StepActivities",
    "StepInput",
    "TemporalActivityCatalog",
]
