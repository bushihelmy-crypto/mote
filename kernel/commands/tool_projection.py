"""Projection of materialized tools into command-protocol representations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mote.contracts.tool.catalog import MaterializedToolCatalog
from mote.kernel.commands.contracts import ToolProjectionContext


@dataclass(frozen=True, slots=True)
class ToolProjection:
    definitions: tuple[dict[str, Any], ...]
    fingerprint: str


def project_tools(
    catalog: MaterializedToolCatalog,
    context: ToolProjectionContext,
) -> ToolProjection:
    if context.protocol == "native":
        definitions = tuple(
            {
                "name": item.name,
                "description": item.description,
                "input_schema": item.input_schema,
                **({"defer_loading": True} if item.defer_loading else {}),
            }
            for item in catalog.definitions
        )
    elif context.protocol == "xml":
        definitions = tuple(
            {
                "name": item.name,
                "description": item.description,
                "parameters": item.input_schema,
            }
            for item in catalog.definitions
        )
    else:
        raise ValueError(f"unsupported command protocol {context.protocol!r}")
    fingerprint = (
        f"{context.protocol}:{context.protocol_version}:" f"{context.capability_fingerprint}:{catalog.fingerprint}"
    )
    return ToolProjection(definitions, fingerprint)


__all__ = ["ToolProjection", "project_tools"]
