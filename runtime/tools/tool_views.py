"""Read-only catalog and policy projections exposed by ToolExecutor."""

from __future__ import annotations

from mote.contracts.schema import DurableConfig, EffectLedgerConfig, ToolResultLimitConfig
from mote.runtime.ledger import RunJournal
from mote.runtime.tools.effect_ledger import EffectLedger
from mote.runtime.tools.tool_catalog import BoundToolCatalog, NativeToolCatalog, XmlToolCatalog


class ToolExecutorViews:
    _catalog: BoundToolCatalog
    _limit_config: ToolResultLimitConfig
    _ledger_config: EffectLedgerConfig
    _durable_config: DurableConfig
    _ledger: EffectLedger | None
    _journal: RunJournal | None

    def prepare(self) -> None:
        ...

    def xml_tool_schemas(self) -> dict[str, dict]:
        self.prepare()
        if not isinstance(self._catalog, XmlToolCatalog):
            raise TypeError("Native ToolExecutor has no XML tool catalog")
        return self._catalog.schemas_for("builtin")

    def mcp_tool_schemas(self) -> dict[str, dict]:
        """Return this executor's protocol-specific MCP reminder definitions."""

        self.prepare()
        if isinstance(self._catalog, XmlToolCatalog):
            return self._catalog.schemas_for("mcp")
        if isinstance(self._catalog, NativeToolCatalog):
            return self._catalog.schemas_for("mcp")
        raise TypeError("ToolExecutor has no protocol-specific tool catalog")

    def xml_pipeline_tool_schemas(self) -> dict[str, dict]:
        self.prepare()
        if not isinstance(self._catalog, XmlToolCatalog):
            raise TypeError("Native ToolExecutor has no XML pipeline catalog")
        return self._catalog.schemas_for("pipeline")

    def all_xml_tool_schemas(self) -> dict[str, dict]:
        self.prepare()
        if not isinstance(self._catalog, XmlToolCatalog):
            raise TypeError("Native ToolExecutor has no XML tool catalog")
        return self._catalog.schemas_for(None)

    def tool_names(self) -> list[str]:
        self.prepare()
        return self._catalog.names()

    def reconstructable_tool_names(self) -> frozenset[str]:
        self.prepare()
        return self._catalog.reconstructable_names()

    def tool_alias_names(self, primary: str) -> frozenset[str]:
        self.prepare()
        return self._catalog.names_of(primary)

    def graph_tool_names(self) -> frozenset[str]:
        self.prepare()
        return self._catalog.graph_tool_names()

    def graph_excluded_tool_names(self) -> frozenset[str]:
        self.prepare()
        return self._catalog.graph_excluded_tool_names()

    def deferred_tool_index(self, *, include_revealed: bool = True) -> dict[str, str]:
        self.prepare()
        return self._catalog.deferred_index(include_revealed=include_revealed)

    def deferred_search_index(self) -> dict[str, str]:
        self.prepare()
        return self._catalog.deferred_search_index()

    def split_tool_menu(self) -> dict[str, str]:
        self.prepare()
        return self._catalog.split_tool_menu()

    def describe_deferred_tools(self, names: list[str]) -> dict[str, str]:
        self.prepare()
        return self._catalog.describe_deferred(names)

    def native_tool_specs(
        self,
        provider: str = "anthropic",
        model: str | None = None,
    ) -> list[dict]:
        self.prepare()
        if not isinstance(self._catalog, NativeToolCatalog):
            raise TypeError("XML ToolExecutor has no Native tool specs")
        return self._catalog.native_specs(provider, model)

    def canonical_tool_specs(self, *, include_hidden: bool = True) -> list[dict]:
        self.prepare()
        if not isinstance(self._catalog, NativeToolCatalog):
            raise TypeError("XML ToolExecutor has no canonical Native tool specs")
        return self._catalog.canonical_specs(include_hidden=include_hidden)

    @property
    def limit_config(self) -> ToolResultLimitConfig:
        return self._limit_config

    @property
    def ledger_config(self) -> EffectLedgerConfig:
        return self._ledger_config

    @property
    def ledger(self) -> EffectLedger | None:
        return self._ledger

    @property
    def durable_config(self) -> DurableConfig:
        return self._durable_config

    @property
    def journal(self) -> RunJournal | None:
        return self._journal
