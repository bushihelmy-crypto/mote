"""Read-only catalog and policy projections exposed by ToolExecutor."""

from __future__ import annotations

from mote.contracts.config.tool import ToolResultLimitConfig
from mote.runtime.tools.tool_catalog import BoundToolCatalog, NativeToolCatalog, XmlToolCatalog


class ToolExecutorViews:
    def prepare(self) -> None: ...

    def xml_tool_schemas(self) -> dict[str, dict]:
        self.prepare()
        if not isinstance(self._bound_catalog, XmlToolCatalog):
            raise TypeError("Native ToolExecutor has no XML tool catalog")
        return self._bound_catalog.schemas_for("builtin")

    def mcp_tool_schemas(self) -> dict[str, dict]:
        """Return this executor's protocol-specific MCP reminder definitions."""

        self.prepare()
        if isinstance(self._bound_catalog, XmlToolCatalog):
            return self._bound_catalog.schemas_for("mcp")
        if isinstance(self._bound_catalog, NativeToolCatalog):
            return self._bound_catalog.schemas_for("mcp")
        raise TypeError("ToolExecutor has no protocol-specific tool catalog")

    def xml_pipeline_tool_schemas(self) -> dict[str, dict]:
        self.prepare()
        if not isinstance(self._bound_catalog, XmlToolCatalog):
            raise TypeError("Native ToolExecutor has no XML pipeline catalog")
        return self._bound_catalog.schemas_for("pipeline")

    def all_xml_tool_schemas(self) -> dict[str, dict]:
        self.prepare()
        if not isinstance(self._bound_catalog, XmlToolCatalog):
            raise TypeError("Native ToolExecutor has no XML tool catalog")
        return self._bound_catalog.schemas_for(None)

    def tool_names(self) -> list[str]:
        self.prepare()
        return self._bound_catalog.names()

    @property
    def tool_binding_generation(self) -> int:
        self.prepare()
        return self._bound_catalog.generation

    def reconstructable_tool_names(self) -> frozenset[str]:
        self.prepare()
        return self._bound_catalog.reconstructable_names()

    def tool_alias_names(self, primary: str) -> frozenset[str]:
        self.prepare()
        return self._bound_catalog.names_of(primary)

    def graph_tool_names(self) -> frozenset[str]:
        self.prepare()
        return self._bound_catalog.graph_tool_names()

    def graph_excluded_tool_names(self) -> frozenset[str]:
        self.prepare()
        return self._bound_catalog.graph_excluded_tool_names()

    def deferred_tool_index(self, *, include_revealed: bool = True) -> dict[str, str]:
        self.prepare()
        return self._bound_catalog.deferred_index(include_revealed=include_revealed)

    def deferred_search_index(self) -> dict[str, str]:
        self.prepare()
        return self._bound_catalog.deferred_search_index()

    def split_tool_menu(self) -> dict[str, str]:
        self.prepare()
        return self._bound_catalog.split_tool_menu()

    def describe_deferred_tools(self, names: list[str]) -> dict[str, str]:
        self.prepare()
        return self._bound_catalog.describe_deferred(names)

    def native_tool_specs(
        self,
        provider: str = "anthropic",
    ) -> list[dict]:
        self.prepare()
        if not isinstance(self._bound_catalog, NativeToolCatalog):
            raise TypeError("XML ToolExecutor has no Native tool specs")
        return self._bound_catalog.native_specs(provider)

    def canonical_tool_specs(self, *, include_hidden: bool = True) -> list[dict]:
        self.prepare()
        if not isinstance(self._bound_catalog, NativeToolCatalog):
            raise TypeError("XML ToolExecutor has no canonical Native tool specs")
        return self._bound_catalog.canonical_specs(include_hidden=include_hidden)

    @property
    def _bound_catalog(self) -> BoundToolCatalog:
        raise NotImplementedError

    @property
    def limit_config(self) -> ToolResultLimitConfig:
        raise NotImplementedError
