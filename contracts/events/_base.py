"""Shared strict-shape validation for explicit durable event codecs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from mote.contracts.events.envelope import JsonValue


@dataclass(frozen=True)
class DurableFact:
    def payload(self) -> dict[str, JsonValue]:
        raise NotImplementedError(f"{type(self).__name__} must define an explicit payload encoder")

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> Self:
        raise NotImplementedError(f"{cls.__name__} must define an explicit payload decoder")
