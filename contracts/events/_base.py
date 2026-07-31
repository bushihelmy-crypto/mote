"""Shared event serialization primitives."""

from __future__ import annotations

from dataclasses import fields
from typing import Any, Self


class DurableFact:
    def payload(self) -> dict[str, Any]:
        return dict(vars(self))

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        names = {item.name for item in fields(cls)}
        if set(payload) != names:
            raise ValueError(f"{cls.__name__} payload fields must be exactly {sorted(names)!r}")
        return cls(**payload)
