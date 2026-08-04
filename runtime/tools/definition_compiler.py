"""Canonical semantic identity compiler for executable Tool definitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mote.contracts.events.envelope import JsonValue, freeze_json, thaw_json
from mote.contracts.tool import CommandProtocol, ToolEffect
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.provider_definitions import NativeToolDefinition, ToolDefinition, XmlToolDefinition

_DEFINITION_SCHEMA = "mote.tool-definition/v1"
_CATALOG_SCHEMA = "mote.tool-catalog/v1"


@dataclass(frozen=True, slots=True)
class CompiledToolDefinition:
    protocol: CommandProtocol
    name: str
    aliases: tuple[str, ...]
    description: str
    input_schema: Mapping[str, JsonValue]
    rendered_schema: Mapping[str, JsonValue]
    execution_kind: str
    effect: ToolEffect
    approval_required: bool
    approval_identity: str
    source_identity: str
    semantic_identity: str


def compile_tool_definition(
    definition: ToolDefinition,
    capability: Any,
    *,
    approval_identity: str,
) -> CompiledToolDefinition:
    """Compile one bound wire definition from declared, observable fields."""

    if not definition.source_identity:
        raise ValueError(f"tool definition '{definition.name}' has no source identity")
    rendered = definition.render(capability)
    if rendered.get("name") != definition.name:
        raise ValueError(f"tool definition '{definition.name}' rendered a different canonical name")
    if rendered.get("description") != definition.description:
        raise ValueError(f"tool definition '{definition.name}' rendered a different description")
    schema_key = "parameters" if isinstance(definition, XmlToolDefinition) else "input_schema"
    schema = rendered.get(schema_key)
    if not isinstance(schema, dict):
        raise TypeError(f"tool definition '{definition.name}' rendered a non-object input schema")
    effect = capability.resolve_effect()
    if not isinstance(effect, ToolEffect):
        raise TypeError(f"tool definition '{definition.name}' returned an invalid effect")
    aliases = tuple(sorted(definition.aliases))
    frozen_schema = freeze_json(schema, path=f"tool.{definition.name}.input_schema")
    frozen_rendered = freeze_json(rendered, path=f"tool.{definition.name}.rendered_schema")
    if not isinstance(frozen_schema, Mapping) or not isinstance(frozen_rendered, Mapping):
        raise TypeError("compiled Tool schemas must be JSON objects")
    payload = {
        "aliases": aliases,
        "approval_identity": approval_identity,
        "approval_required": definition.approval_required,
        "description": definition.description,
        "effect": effect.value,
        "execution_kind": definition.execution_kind.value,
        "input_schema": thaw_json(frozen_schema),
        "name": definition.name,
        "protocol": definition.protocol.value,
        "schema": _DEFINITION_SCHEMA,
        "source_identity": definition.source_identity,
    }
    return CompiledToolDefinition(
        protocol=definition.protocol,
        name=definition.name,
        aliases=aliases,
        description=definition.description,
        input_schema=frozen_schema,
        rendered_schema=frozen_rendered,
        execution_kind=definition.execution_kind.value,
        effect=effect,
        approval_required=definition.approval_required,
        approval_identity=approval_identity,
        source_identity=definition.source_identity,
        semantic_identity=_content_identity(payload),
    )


def compile_tool_catalog_identity(definitions: tuple[CompiledToolDefinition, ...]) -> str:
    """Return the order-independent content identity of a complete definition set."""

    ordered = sorted(definitions, key=lambda item: (item.name, item.protocol.value, item.semantic_identity))
    owners: dict[str, str] = {}
    for definition in ordered:
        for dispatch_name in (definition.name, *definition.aliases):
            previous = owners.get(dispatch_name)
            if previous is not None:
                raise ValueError(
                    f"tool dispatch name '{dispatch_name}' belongs to both " f"'{previous}' and '{definition.name}'"
                )
            owners[dispatch_name] = definition.name
    return _content_identity(
        {
            "definitions": [item.semantic_identity for item in ordered],
            "schema": _CATALOG_SCHEMA,
        }
    )


def python_tool_source_identity(capability_type: type[BaseTool]) -> str:
    """Stable source declaration identity; behavior fields are hashed separately."""

    definition_name = capability_type.name
    if not isinstance(definition_name, str) or not definition_name:
        raise TypeError("Python tool type must declare a stable non-empty name")
    explicit_version = capability_type.definition_version
    if not isinstance(explicit_version, str) or not explicit_version:
        raise TypeError(f"tool type '{capability_type.__name__}' has an invalid definition_version")
    return compile_tool_source_identity(
        "python",
        {
            "definition_name": definition_name,
            "version": explicit_version,
        },
    )


def compile_tool_source_identity(kind: str, payload: object) -> str:
    if not kind or not kind.isascii():
        raise ValueError("tool source kind must be a non-empty ASCII identifier")
    return f"mote.tool-source.{kind}/v1:{_content_identity(payload)}"


def _content_identity(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TypeError("tool definition contains a non-JSON canonical field") from error
    return f"sha256-{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "CompiledToolDefinition",
    "compile_tool_catalog_identity",
    "compile_tool_definition",
    "compile_tool_source_identity",
    "python_tool_source_identity",
]
