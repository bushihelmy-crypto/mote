"""Narrow compatibility operation owners used by the HTTP projection."""

from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue

from mote.contracts.artifact import ArtifactRef


class UnaryCompatibilityOwner(Protocol):
    async def execute(self, operation: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        ...


class DurableCompatibilityOwner(Protocol):
    async def execute(self, operation: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        ...

    async def list(self, operation: str, query: dict[str, str]) -> dict[str, JsonValue]:
        ...

    async def resource(self, operation: str, resource_id: str) -> dict[str, JsonValue]:
        ...

    async def content(self, resource_id: str) -> ArtifactRef:
        ...


class ArtifactCompatibilityOwner(Protocol):
    async def upload(self, operation: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        ...

    async def upload_bytes(
        self,
        operation: str,
        content: bytes,
        *,
        filename: str,
        content_type: str,
        fields: dict[str, str],
    ) -> dict[str, JsonValue]:
        ...


__all__ = [
    "ArtifactCompatibilityOwner",
    "DurableCompatibilityOwner",
    "UnaryCompatibilityOwner",
]
