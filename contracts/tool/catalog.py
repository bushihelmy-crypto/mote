"""Protocol-neutral materialized tool catalog contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from mote.contracts.artifact import ArtifactRef
from mote.contracts.tool.result import FileChange, ToolMedia

DispatchValueT = TypeVar("DispatchValueT", covariant=True)


@dataclass(frozen=True, slots=True)
class ToolCatalogIdentity:
    catalog_id: str
    version: str


@dataclass(frozen=True, slots=True)
class MaterializedToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    semantic_identity: str
    effect: str = "pure"
    defer_loading: bool = False


@dataclass(frozen=True, slots=True)
class MaterializedToolCatalog:
    identity: ToolCatalogIdentity
    revision: int
    definitions: tuple[MaterializedToolDefinition, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ToolBindingSnapshot:
    snapshot_id: str
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
    arguments: dict[str, Any]
    call_id: str = ""


@dataclass(frozen=True, slots=True)
class ToolDispatchResult(Generic[DispatchValueT]):
    success: bool
    value: DispatchValueT | None = None
    conflict: str = ""


class ToolExecutionOutcome(Protocol):
    output: str
    success: bool
    data: object
    media: list[ToolMedia]
    artifacts: list[ArtifactRef]
    file_changes: list[FileChange]
    terminate: bool
    retention: str | None
    resource_path: str | None


class ToolExecutionPort(Protocol[DispatchValueT]):
    async def dispatch(self, request: ToolDispatchRequest) -> ToolDispatchResult[DispatchValueT]:
        ...

    def release(self, snapshot: ToolBindingSnapshot) -> bool:
        ...


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
