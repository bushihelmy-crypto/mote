"""Immutable tool catalogs and their Runtime definition adapters."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from mote.runtime.tools.definition_compiler import compile_tool_catalog_identity, compile_tool_definition
from mote.runtime.tools.definitions import native_definition, xml_definition
from mote.runtime.tools.provider import NativeToolset, XmlToolset


def _validate_tool_type(tool_type: type) -> None:
    if not getattr(tool_type, "stateful", False):
        return
    requires = getattr(tool_type, "requires", ())
    if "get_runtime_host" not in requires:
        raise TypeError(f"stateful tool '{tool_type.__name__}' must declare the " "get_runtime_host capability")
    if "handoff_runtime" not in requires:
        raise TypeError(f"stateful tool '{tool_type.__name__}' must declare the " "handoff_runtime capability")
    if "action" not in inspect.signature(tool_type.call).parameters:
        raise TypeError(f"stateful tool '{tool_type.__name__}' must expose handoff through " "its action parameter")


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    """Immutable, content-addressed capability-type snapshot.

    A catalog belongs to one Application generation. Toolsets copy definitions
    from it during Product assembly, so later plugin discovery cannot mutate an
    already-running session's dispatch surface.
    """

    version: str
    _types: tuple[type, ...]

    @classmethod
    def from_types(cls, types: Iterable[type]) -> "ToolCatalog":
        unique: dict[str, type] = {}
        owners: dict[str, str] = {}
        for tool_type in types:
            _validate_tool_type(tool_type)
            name = getattr(tool_type, "name", "") or tool_type.__name__
            existing = unique.get(name)
            if existing is not None and existing is not tool_type:
                raise ValueError(f"tool name '{name}' is declared more than once")
            unique[name] = tool_type
            for dispatch_name in (name, *getattr(tool_type, "aliases", ())):
                owner = owners.get(dispatch_name)
                if owner is not None and owner != name:
                    raise ValueError(f"tool dispatch name '{dispatch_name}' belongs to both '{owner}' and '{name}'")
                owners[dispatch_name] = name
        ordered = tuple(unique[name] for name in sorted(unique))
        compiled = tuple(
            compile_tool_definition(
                definition := native_definition(tool_type),
                object.__new__(tool_type),
                approval_identity="none",
            )
            for tool_type in ordered
        )
        version = compile_tool_catalog_identity(compiled)
        return cls(version=version, _types=ordered)

    def get(self, name: str) -> type | None:
        for tool_type in self._types:
            if name == tool_type.name or name in getattr(tool_type, "aliases", ()):
                return tool_type
        return None

    def all_tools(self) -> dict[str, type]:
        return {tool_type.name: tool_type for tool_type in self._types}

    def with_types(self, *types: type) -> "ToolCatalog":
        merged = self.all_tools()
        for tool_type in types:
            name = getattr(tool_type, "name", "") or tool_type.__name__
            existing = merged.get(name)
            if existing is not None and existing is not tool_type:
                raise ValueError(f"tool name '{name}' already belongs to '{existing.__name__}'")
            merged[name] = tool_type
        return type(self).from_types(merged.values())


def _included_types(catalog: ToolCatalog, include: frozenset[str] | None) -> tuple[type, ...]:
    tools = catalog.all_tools()
    if include is not None:
        tools = {name: tool for name, tool in tools.items() if name in include}
    return tuple(tools.values())


def _xml_definitions(
    catalog: ToolCatalog,
    include: frozenset[str] | None,
    capability_factories: Mapping[str, Callable[[], Any]],
    descriptions: Mapping[str, str],
):
    return (
        xml_definition(
            tool,
            capability_factories.get(tool.name),
            description=descriptions.get(tool.name),
        )
        for tool in _included_types(catalog, include)
    )


def _native_definitions(
    catalog: ToolCatalog,
    include: frozenset[str] | None,
    capability_factories: Mapping[str, Callable[[], Any]],
    descriptions: Mapping[str, str],
):
    return (
        native_definition(
            tool,
            capability_factories.get(tool.name),
            description=descriptions.get(tool.name),
        )
        for tool in _included_types(catalog, include)
    )


class XmlCatalogToolset(XmlToolset):
    """XML definitions backed by one immutable Application catalog."""

    def __init__(
        self,
        *,
        id: str,
        catalog: ToolCatalog,
        version: str | None = None,
        prepare: Callable[[], None] | None = None,
        include: frozenset[str] | None = None,
        capability_factories: Mapping[str, Callable[[], Any]] | None = None,
        descriptions: Mapping[str, str] | None = None,
    ) -> None:
        factories = dict(capability_factories or {})
        rendered_descriptions = dict(descriptions or {})
        super().__init__(
            id,
            lambda: _xml_definitions(catalog, include, factories, rendered_descriptions),
            version=version or catalog.version,
            prepare=prepare,
        )


class NativeCatalogToolset(NativeToolset):
    """Native definitions backed by one immutable Application catalog."""

    def __init__(
        self,
        *,
        id: str,
        catalog: ToolCatalog,
        version: str | None = None,
        prepare: Callable[[], None] | None = None,
        include: frozenset[str] | None = None,
        capability_factories: Mapping[str, Callable[[], Any]] | None = None,
        descriptions: Mapping[str, str] | None = None,
    ) -> None:
        factories = dict(capability_factories or {})
        rendered_descriptions = dict(descriptions or {})
        super().__init__(
            id,
            lambda: _native_definitions(catalog, include, factories, rendered_descriptions),
            version=version or catalog.version,
            prepare=prepare,
        )


__all__ = [
    "NativeCatalogToolset",
    "ToolCatalog",
    "XmlCatalogToolset",
]
