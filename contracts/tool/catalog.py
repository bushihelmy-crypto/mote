"""Protocol-neutral materialized tool catalog contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from mote.contracts.artifact import ArtifactRef
from mote.contracts.events.envelope import JsonValue, freeze_json
from mote.contracts.tool.result import FileChange, ToolMedia, ToolPayload

DispatchValueT = TypeVar("DispatchValueT", covariant=True)


@dataclass(frozen=True, slots=True)
class ToolCatalogIdentity:
    catalog_id: str
    version: str


@dataclass(frozen=True, slots=True)
class MaterializedToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, JsonValue]
    semantic_identity: str
    effect: str = "pure"
    defer_loading: bool = False

    def __post_init__(self) -> None:
        frozen = freeze_json(self.input_schema, path="materialized tool input_schema")
        if not isinstance(frozen, Mapping):
            raise TypeError("materialized tool input_schema must be an object")
        object.__setattr__(self, "input_schema", frozen)


@dataclass(frozen=True, slots=True)
class MaterializedToolCatalog:
    identity: ToolCatalogIdentity
    revision: int
    definitions: tuple[MaterializedToolDefinition, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ToolBindingSnapshot:
    snapshot_id: str
    composition_generation_id: str
    catalog: MaterializedToolCatalog
    target_id: str
    capability_fingerprint: str
    provider_descriptor: str
    registry_revision: int
    retention_lease_id: str


@dataclass(frozen=True, slots=True)
class ToolDispatchRequest:
    snapshot_id: str
    registry_revision: int
    tool_name: str
    arguments: Mapping[str, JsonValue]
    call_id: str = ""

    def __post_init__(self) -> None:
        frozen = freeze_json(self.arguments, path="tool dispatch arguments")
        if not isinstance(frozen, Mapping):
            raise TypeError("tool dispatch arguments must be an object")
        object.__setattr__(self, "arguments", frozen)


@dataclass(frozen=True, slots=True)
class ToolDispatchResult(Generic[DispatchValueT]):
    success: bool
    value: DispatchValueT | None = None
    conflict: str = ""


class ToolExecutionOutcome(Protocol):
    output: str
    success: bool
    payload: ToolPayload | None
    execution_value: object | None
    media: tuple[ToolMedia, ...]
    artifacts: tuple[ArtifactRef, ...]
    file_changes: tuple[FileChange, ...]
    terminate: bool
    retention: str | None
    resource_path: str | None


class ToolExecutionPort(Protocol[DispatchValueT]):
    async def dispatch(self, request: ToolDispatchRequest) -> ToolDispatchResult[DispatchValueT]: ...

    def release(self, snapshot: ToolBindingSnapshot) -> bool: ...


__all__ = [
    "MaterializedToolCatalog",
    "MaterializedToolDefinition",
    "ToolBindingSnapshot",
    "ToolCatalogIdentity",
    "ToolDispatchRequest",
    "ToolDispatchResult",
    "ToolExecutionOutcome",
    "ToolExecutionPort",
]
