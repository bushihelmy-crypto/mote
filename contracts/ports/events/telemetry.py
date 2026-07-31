"""Bounded loss-tolerant observation contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import NewType, Protocol, TypeGuard, TypeVar

TelemetryIdentity = NewType("TelemetryIdentity", str)

MAX_TELEMETRY_CAPACITY = 65_536
MAX_TELEMETRY_IDENTITY_BYTES = 255
_IDENTITY_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")


class TelemetryOverflow(StrEnum):
    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    COALESCE = "coalesce"


@dataclass(frozen=True)
class TelemetrySubscriptionSpec:
    identity: TelemetryIdentity
    capacity: int
    overflow: TelemetryOverflow

    def __post_init__(self) -> None:
        if type(self.identity) is not str or _IDENTITY_PATTERN.fullmatch(self.identity) is None:
            raise ValueError("telemetry identity must be stable and namespaced")
        if len(self.identity.encode("utf-8")) > MAX_TELEMETRY_IDENTITY_BYTES:
            raise ValueError("telemetry identity exceeds its byte bound")
        if type(self.capacity) is not int or not 1 <= self.capacity <= MAX_TELEMETRY_CAPACITY:
            raise ValueError("telemetry capacity is outside its bound")


EventT_contra = TypeVar("EventT_contra", contravariant=True)
EventT_co = TypeVar("EventT_co", covariant=True)


class TelemetryHandler(Protocol[EventT_contra]):
    async def handle(self, event: EventT_contra) -> None:
        ...


class SyncTelemetryHandler(Protocol[EventT_contra]):
    def handle_sync(self, event: EventT_contra) -> None:
        ...


class EventNarrower(Protocol[EventT_co]):
    def __call__(self, event: object) -> TypeGuard[EventT_co]:
        ...


__all__ = [
    "MAX_TELEMETRY_CAPACITY",
    "MAX_TELEMETRY_IDENTITY_BYTES",
    "EventNarrower",
    "SyncTelemetryHandler",
    "TelemetryHandler",
    "TelemetryIdentity",
    "TelemetryOverflow",
    "TelemetrySubscriptionSpec",
]
