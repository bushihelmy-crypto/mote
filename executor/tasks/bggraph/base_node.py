"""BaseNode — abstract base class for BgGraph nodes.

Mirrors ``BaseTool``'s pattern (``name`` / ``description`` / ``call()`` with
auto-extracted metadata from docstrings) but does NOT share an inheritance
hierarchy. Both classes share the leaf-level parsing utilities from
``metagpt.common.utils.docstring``.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Optional, get_type_hints

from metagpt.common.utils.docstring import first_line, parse_section
from metagpt.executor.tasks.bggraph.types import GraphState, Stage
from metagpt.executor.tool_spec_adapter import annotation_to_json_schema

# ---------------------------------------------------------------------------
# Param source marker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class From:
    """Declares a node param's data source on a ``BaseNode.call`` signature.

    A bare type annotation can carry the param's *type* but not where its value
    comes from. Wrap the annotation in :data:`typing.Annotated` with a ``From``
    marker to declare the ``from`` source for a signature-only param::

        from typing import Annotated

        class Doubler(BaseNode):
            async def call(
                self,
                state,
                val: Annotated[int, From("a")],            # upstream node 'a' output
                prompt: Annotated[str, From("$input.text")] # graph input 'text'
            ) -> Stage:
                ...

    ``source`` follows the same convention as the docstring ``Params:`` / explicit
    ``params=`` ``from`` field: ``"$input.<field>"`` for a graph input, or
    ``"<node>"`` / ``"<node>.<key>"`` for an upstream node's output.
    """

    source: str


def _split_annotated(hint) -> tuple[object, str]:
    """Split an annotation into ``(actual_type, from_source)``.

    Extracts a :class:`From` marker from an ``Annotated[...]`` hint (the source
    is "" when absent). Plain hints pass through unchanged with an empty source.
    """
    if hint is not None and hasattr(hint, "__metadata__"):
        source = ""
        for meta in hint.__metadata__:
            if isinstance(meta, From):
                source = meta.source
                break
        return hint.__origin__, source
    return hint, ""


# ---------------------------------------------------------------------------
# Type resolution
# ---------------------------------------------------------------------------

_BUILTIN_TYPE_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "bytes": bytes,
    "None": type(None),
    "none": type(None),
}


def _resolve_type(type_str: str) -> type | None:
    """Resolve a type-hint string to its Python type (builtins only).

    Returns None for unrecognized strings — callers skip type-checking when None.
    """
    return _BUILTIN_TYPE_MAP.get(type_str.strip())


# ---------------------------------------------------------------------------
# BaseNode
# ---------------------------------------------------------------------------


class BaseNode(ABC):
    """BgGraph node base class.

    Subclass, set ``name``, optionally ``description`` / ``timeout``, and
    implement ``call(state) -> Stage``.  Register via ``graph.add_node(MyNode)``
    or ``graph.add_node(MyNode())``.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    timeout: ClassVar[Optional[float]] = None

    @abstractmethod
    async def call(self, state: GraphState) -> Stage:
        """Node execution entry point. Must return a Stage."""

    @classmethod
    def get_description(cls) -> str:
        """ClassVar ``description`` wins; fallback: ``call()`` then class docstring first line."""
        if cls.description:
            return cls.description
        call_desc = first_line(cls.call)
        if call_desc:
            return call_desc
        return first_line(cls)

    @classmethod
    def get_params(cls) -> dict[str, dict]:
        """Auto-extract param metadata from ``call()``'s docstring + typed kwargs.

        Precedence (signature is the structured authority, docstring the prose
        fallback): a signature type hint overrides a docstring ``type``, and a
        non-empty ``Annotated[..., From(src)]`` overrides a docstring ``from``.
        ``desc`` comes from the docstring only.
        """
        params = _parse_params_from_docstring(cls.call)
        # Merge typed kwargs from call() signature (skip self/state/var-positional/var-keyword).
        # include_extras=True keeps Annotated metadata so From() survives.
        try:
            hints = get_type_hints(cls.call, include_extras=True)
        except Exception:
            hints = {}
        sig = inspect.signature(cls.call)
        for pname, p in sig.parameters.items():
            if pname in ("self", "state"):
                continue
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            actual_type, source = _split_annotated(hints.get(pname))
            if pname not in params:
                if actual_type is not None:
                    params[pname] = {"from": source, "desc": "", "type": actual_type}
            else:
                if actual_type is not None:
                    # Signature type overrides docstring type
                    params[pname]["type"] = actual_type
                if source:
                    # A non-empty Annotated From overrides the docstring from;
                    # an absent marker (source == "") never clobbers it.
                    params[pname]["from"] = source
        return params

    @classmethod
    def get_json_schema(cls) -> dict:
        """Build a JSON Schema for this node's declared params.

        Returns ``{"type": "object", "properties": {...}, "required": [...]}``
        derived from ``get_params()`` types and descriptions. Params without a
        declared type map to ``{"type": "string"}`` (permissive fallback).

        Reuses :func:`~metagpt.executor.tool_spec_adapter.annotation_to_json_schema`
        so the type mapping is consistent with the tool system.
        """

        params = cls.get_params()
        properties: dict[str, dict] = {}
        required: list[str] = []

        for name, info in params.items():
            ptype = info.get("type")
            if ptype is not None:
                prop = annotation_to_json_schema(ptype)
            else:
                prop = {"type": "string"}
            desc = info.get("desc")
            if desc:
                prop["description"] = desc
            source = info.get("from")
            if source:
                prop["x-source"] = source
            properties[name] = prop
            # All declared params are "required" in the schema sense (no default)
            required.append(name)

        schema: dict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema


# ---------------------------------------------------------------------------
# Docstring param parser
# ---------------------------------------------------------------------------


def _parse_params_from_docstring(fn) -> dict[str, dict]:
    """Parse ``Params:`` section into ``{name: {"from": source, "desc": desc, "type": type|None}}``.

    The convention is::

        Params:
            name: source — type — description     (3-segment)
            name: source — description            (2-segment, type=None)
            name: source                          (1-segment, type=None, desc="")

    Separator between segments is `` — `` (em-dash), `` – ``
    (en-dash), or `` - `` (space-hyphen-space).
    """
    doc = inspect.getdoc(fn) or (fn.__doc__ if hasattr(fn, "__doc__") else None)
    entries = parse_section(doc, "Params")
    result: dict[str, dict] = {}
    for name, rest in entries:
        parts = _split_param_rest(rest)
        if len(parts) >= 3:
            # source — type — desc
            source = parts[0]
            resolved = _resolve_type(parts[1])
            desc = " — ".join(parts[2:])
            result[name] = {"from": source, "desc": desc, "type": resolved}
        elif len(parts) == 2:
            # source — desc (no type)
            result[name] = {"from": parts[0], "desc": parts[1], "type": None}
        else:
            # source only
            result[name] = {"from": parts[0], "desc": "", "type": None}
    return result


def _split_param_rest(rest: str) -> list[str]:
    """Split a param's rest string on em-dash/en-dash/space-hyphen-space separators."""
    for sep in (" — ", " – ", " - "):
        if sep in rest:
            return [part.strip() for part in rest.split(sep)]
    return [rest.strip()]
