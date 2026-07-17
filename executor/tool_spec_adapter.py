"""tool_spec_adapter — convert Mote tool schemas to native tool-use specs.

The XML command protocol describes tools as free-form text (name + signature +
a natural-language ``Args:`` block). Native tool-use APIs instead require a
structured JSON Schema for each tool's parameters. This module builds that JSON
Schema from a tool's ``call()`` signature + Google-style docstring, then wraps
it into the provider-specific envelope (Anthropic / OpenAI).

Pure functions, zero side effects — the schema builder for the native-tool-use
channel. ``build_json_schema`` backs :meth:`BaseTool.get_native_schema` (each
tool's ``input_schema``) and ``to_native_tool_specs`` backs
:meth:`ToolExecutor.get_native_tool_specs` (the provider envelope). The XML
command protocol does not use this module; it describes tools as free-form text.

Usage:
    from mote.executor.tool_spec_adapter import build_json_schema, to_native_tool_specs

    schema = build_json_schema(MyTool.call)              # {type:object, properties, required}
    specs = to_native_tool_specs({name: native_schema}, "anthropic")  # provider envelope
"""
from __future__ import annotations

import inspect
import types
import typing
from typing import Any, Callable, Union

from pydantic import BaseModel

from mote.common.utils.docstring import parse_section

# JSON Schema primitive for each Python type. Mirrors stream_xml.PythonObjectParser.types
# but maps to JSON Schema's "object" (not the parser's internal "map").
_PY_TO_JSON_TYPE: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}

# Parameters that are framework plumbing, never LLM-facing arguments.
_SKIP_PARAMS = frozenset({"self", "cls", "args", "kwargs"})


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Strip Optional/Union[..., None]; return (inner_annotation, is_optional).

    ``Optional[X]`` is ``Union[X, None]``. For a multi-arm Union we keep the
    first non-None arm (best-effort; native schema does not need exhaustive
    union typing). Returns (X, True) when None was a member.

    Also handles PEP 604 ``X | Y`` unions (``types.UnionType``, Python 3.10+).
    """
    origin = typing.get_origin(annotation)
    if origin is Union or isinstance(annotation, types.UnionType):
        arms = [a for a in typing.get_args(annotation) if a is not types.NoneType]  # noqa: E721  # identity check
        is_optional = len(arms) != len(typing.get_args(annotation))
        inner = arms[0] if arms else str
        return inner, is_optional
    return annotation, False


def _json_type(annotation: Any) -> dict:
    """Map a Python annotation to a JSON Schema type fragment.

    Falls back to {"type": "string"} for anything unrecognized — native APIs
    require *a* type, and an over-broad string is safer than a hard failure.
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return {"type": "string"}

    inner, _ = _unwrap_optional(annotation)
    origin = typing.get_origin(inner)

    # Parameterized generics: list[str], dict[str, int], etc.
    if origin in (list, set, tuple):
        args = typing.get_args(inner)
        item = _json_type(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item}
    if origin is dict:
        return {"type": "object"}

    # Pydantic model → its full JSON Schema. This is the "auto" path for tools
    # with structured params: annotate `call(self, *, items: list[MyModel])` (or
    # a bare `MyModel`) and the nested object schema (properties + required) is
    # derived from the model — no hand-written schema or get_native_schema()
    # override needed. Override remains the escape hatch for dynamic params (MCP).
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        return inner.model_json_schema()

    if inner in _PY_TO_JSON_TYPE:
        return {"type": _PY_TO_JSON_TYPE[inner]}

    # Unknown / unhashable (e.g. typing constructs, Path) → permissive string.
    return {"type": "string"}


# Public alias — used by bggraph.base_node for node-level JSON Schema generation.
annotation_to_json_schema = _json_type


def _parse_arg_descriptions(docstring: str | None) -> dict[str, str]:
    """Extract per-parameter descriptions from a Google-style ``Args:`` block.

    Returns {param_name: description}. Delegates to the shared
    ``parse_section`` utility for the actual parsing.
    """

    return dict(parse_section(docstring, "Args"))


def build_json_schema(call_fn: Callable) -> dict:
    """Build a JSON Schema ``object`` for a tool's ``call()`` parameters.

    Inspects the signature for parameter names, types, and defaults, and the
    docstring for descriptions. Skips ``self``/``cls``/``*args``/``**kwargs``.
    A parameter with no default becomes ``required``.

    Structured params: a parameter annotated with a pydantic ``BaseModel`` (or
    ``list[Model]``) expands to that model's full nested JSON Schema, so tools
    with rich inputs get a correct schema from the type alone — no hand-written
    schema or ``get_native_schema()`` override required.

    Returns {"type": "object", "properties": {...}, "required": [...]}.
    When the signature exposes no usable parameters (e.g. ``call(self,
    **kwargs)``), returns an empty-properties object — callers (MCP) should
    prefer a pre-existing JSON Schema in that case.
    """
    try:
        sig = inspect.signature(call_fn)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}}

    # ``from __future__ import annotations`` (PEP 563) stringizes annotations, so
    # ``param.annotation`` would be e.g. the string "float" instead of the float
    # type. Resolve real types via get_type_hints; fall back to the raw signature
    # annotation if resolution fails (forward refs, missing names).
    try:
        hints = typing.get_type_hints(call_fn)
    except Exception:  # noqa: BLE001 — best-effort; degrade to raw annotations
        hints = {}

    descriptions = _parse_arg_descriptions(inspect.getdoc(call_fn))
    properties: dict[str, dict] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in _SKIP_PARAMS:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        annotation = hints.get(name, param.annotation)
        prop = _json_type(annotation)
        if name in descriptions and descriptions[name]:
            prop["description"] = descriptions[name]
        properties[name] = prop

        if param.default is inspect.Parameter.empty:
            required.append(name)

    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def to_native_tool_specs(tool_schemas: dict[str, dict], provider: str = "anthropic") -> list[dict]:
    """Wrap a {name: schema} mapping into provider-specific native tool specs.

    ``tool_schemas`` values must each carry at least ``name``, ``description``,
    and a JSON Schema ``input_schema`` (the structured params). Use
    ToolExecutor.get_native_tool_specs() to obtain a ready mapping; this fn is
    the pure envelope step.

    provider:
      - "anthropic":        {"name", "description", "input_schema": <schema>}
      - "openai":           {"type":"function", "function":{"name","description","parameters":<schema>}}
      - "openai_responses": {"type":"function", "name", "description", "parameters":<schema>}
        (the Responses API uses a FLAT function shape, distinct from Chat
        Completions' nested ``function`` object.)
    """
    provider = provider.lower()
    specs: list[dict] = []
    for schema in tool_schemas.values():
        name = schema["name"]
        description = schema.get("description", "") or ""
        params = schema.get("input_schema") or {"type": "object", "properties": {}}
        if provider == "openai_responses":
            specs.append({"type": "function", "name": name, "description": description, "parameters": params})
        elif provider == "openai":
            specs.append(
                {
                    "type": "function",
                    "function": {"name": name, "description": description, "parameters": params},
                }
            )
        else:  # anthropic (default)
            specs.append({"name": name, "description": description, "input_schema": params})
    return specs
