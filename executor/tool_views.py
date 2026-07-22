"""Read-only catalog and policy projections exposed by ToolExecutor."""

from __future__ import annotations

from mote.common.ledger import RunJournal
from mote.common.schema import DurableConfig, EffectLedgerConfig, ToolResultLimitConfig
from mote.executor.effect_ledger import EffectLedger


class ToolExecutorViews:
    _catalog: object
    _limit_config: ToolResultLimitConfig
    _ledger_config: EffectLedgerConfig
    _durable_config: DurableConfig
    _ledger: EffectLedger | None
    _journal: RunJournal | None

    def prepare(self) -> None:
        ...

    def get_tool_schemas(self) -> dict[str, dict]:
        self.prepare()
        return self._catalog.schemas_for("builtin")

    def get_mcp_tool_schemas(self) -> dict[str, dict]:
        self.prepare()
        return self._catalog.schemas_for("mcp")

    def get_pipeline_tool_schemas(self) -> dict[str, dict]:
        self.prepare()
        return self._catalog.schemas_for("pipeline")

    def get_all_tool_schemas(self) -> dict[str, dict]:
        self.prepare()
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

    def get_native_tool_specs(
        self,
        provider: str = "anthropic",
        model: str | None = None,
    ) -> list[dict]:
        self.prepare()
        return self._catalog.native_specs(provider, model)

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
