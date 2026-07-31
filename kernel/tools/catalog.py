"""Deterministic composition algebra for protocol-neutral tools."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.tool.catalog import ToolCatalogIdentity
from mote.kernel.tools.definitions import ToolDefinition


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    identity: ToolCatalogIdentity
    definitions: tuple[ToolDefinition, ...]

    def __post_init__(self) -> None:
        owners: dict[str, str] = {}
        for definition in self.definitions:
            for name in definition.names:
                if name in owners:
                    raise ValueError(f"tool name {name!r} conflicts with {owners[name]!r}")
                owners[name] = definition.semantic_identity

    def select(self, names: frozenset[str]) -> "ToolCatalog":
        return ToolCatalog(
            self.identity,
            tuple(item for item in self.definitions if item.name in names),
        )

    def renamed(self, names: dict[str, str]) -> "ToolCatalog":
        return ToolCatalog(
            self.identity,
            tuple(item.renamed(names.get(item.name, item.name)) for item in self.definitions),
        )

    def combine(self, other: "ToolCatalog") -> "ToolCatalog":
        return ToolCatalog(self.identity, (*self.definitions, *other.definitions))


__all__ = ["ToolCatalog"]
