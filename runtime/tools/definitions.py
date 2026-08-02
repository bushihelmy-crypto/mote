"""Runtime adapters from executable capabilities to explicit wire definitions."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import partial
from typing import Any

from mote.contracts.tool import NativeToolSchema, XmlToolSchema
from mote.contracts.tool.execution import ToolExecutionKind
from mote.kernel.tools.docstrings import description_body, first_line
from mote.kernel.tools.spec_adapter import build_json_schema
from mote.runtime.tools.definition_compiler import python_tool_source_identity
from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition
from mote.runtime.tools.tool_convert import function_docstring_to_schema


def _description(capability_type: type) -> str:
    override = getattr(capability_type, "model_description", None)
    if override is not None:
        rendered = override()
        if rendered is not None:
            return str(rendered)
    return description_body(inspect.getdoc(capability_type.call) or "")


def _summary(capability_type: type) -> str:
    return first_line(_description(capability_type))


def _search_text(capability_type: type) -> str:
    summary = _summary(capability_type)
    keywords = tuple(getattr(capability_type, "keywords", ()))
    return f"{summary} {' '.join(keywords)}" if keywords else summary


def render_xml_capability(capability: Any) -> XmlToolSchema:
    """Render only the scalar/string XML command contract."""

    capability_type = type(capability)
    docstring = inspect.getdoc(capability_type.call) or ""
    return {
        "name": capability_type.name,
        "description": _description(capability_type),
        "parameters": function_docstring_to_schema(capability_type.call, docstring),
    }


def render_native_capability(capability: Any) -> NativeToolSchema:
    """Render the structured JSON Schema contract for provider-native use."""

    capability_type = type(capability)
    return {
        "name": capability_type.name,
        "description": _description(capability_type),
        "input_schema": build_json_schema(capability_type.call),
    }


def _render_xml_with_description(capability: Any, *, description: str) -> XmlToolSchema:
    schema = render_xml_capability(capability)
    schema["description"] = description
    return schema


def _render_native_with_description(capability: Any, *, description: str) -> NativeToolSchema:
    schema = render_native_capability(capability)
    schema["description"] = description
    return schema


def xml_definition(
    capability_type: type,
    capability_factory: Callable[[], Any] | None = None,
    *,
    description: str | None = None,
) -> XmlToolDefinition[Any]:
    resolved_description = description if description is not None else _description(capability_type)
    return XmlToolDefinition(
        name=str(capability_type.name),
        aliases=tuple(getattr(capability_type, "aliases", ())),
        capability_factory=capability_factory or capability_type,
        capability_type=capability_type,
        schema_renderer=(
            partial(_render_xml_with_description, description=resolved_description)
            if description is not None
            else render_xml_capability
        ),
        source_identity=python_tool_source_identity(capability_type),
        description=resolved_description,
        summary=first_line(resolved_description),
        search_text=_search_text_from_description(capability_type, resolved_description),
        execution_kind=getattr(capability_type, "execution_kind", ToolExecutionKind.ATOMIC),
    )


def native_definition(
    capability_type: type,
    capability_factory: Callable[[], Any] | None = None,
    *,
    description: str | None = None,
) -> NativeToolDefinition[Any]:
    resolved_description = description if description is not None else _description(capability_type)
    return NativeToolDefinition(
        name=str(capability_type.name),
        aliases=tuple(getattr(capability_type, "aliases", ())),
        capability_factory=capability_factory or capability_type,
        capability_type=capability_type,
        schema_renderer=(
            partial(_render_native_with_description, description=resolved_description)
            if description is not None
            else render_native_capability
        ),
        source_identity=python_tool_source_identity(capability_type),
        description=resolved_description,
        summary=first_line(resolved_description),
        search_text=_search_text_from_description(capability_type, resolved_description),
        execution_kind=getattr(capability_type, "execution_kind", ToolExecutionKind.ATOMIC),
    )


def _search_text_from_description(capability_type: type, description: str) -> str:
    summary = first_line(description)
    keywords = tuple(getattr(capability_type, "keywords", ()))
    return f"{summary} {' '.join(keywords)}" if keywords else summary


__all__ = [
    "native_definition",
    "render_native_capability",
    "render_xml_capability",
    "xml_definition",
]
