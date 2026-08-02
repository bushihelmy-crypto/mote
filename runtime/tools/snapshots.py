"""Materialization and pinned dispatch for run-scoped tool snapshots."""

from __future__ import annotations

from uuid import uuid4

from mote.contracts.tool.catalog import (
    MaterializedToolCatalog,
    MaterializedToolDefinition,
    ToolBindingSnapshot,
    ToolCatalogIdentity,
    ToolDispatchRequest,
    ToolDispatchResult,
    ToolExecutionOutcome,
)
from mote.runtime.tools.bound_registry import BoundToolRegistry, PinnedToolInvocation
from mote.runtime.tools.definition_compiler import compile_tool_catalog_identity
from mote.runtime.tools.tool_binding import ExecutableToolBinding


class RuntimeToolSnapshotManager:
    def __init__(self, executor) -> None:
        self._executor = executor
        self._registry = BoundToolRegistry()
        self._revision = 0
        self._references: dict[tuple[str, int], int] = {}

    def materialize(self, target, *, include_hidden: bool) -> ToolBindingSnapshot:
        definitions = self._definitions(include_hidden=include_hidden)
        bound_definitions = tuple(self._bound_definition(item.name) for item in definitions)
        fingerprint = compile_tool_catalog_identity(bound_definitions)
        self._revision += 1
        snapshot_id = uuid4().hex
        catalog = MaterializedToolCatalog(
            ToolCatalogIdentity("runtime-tools", fingerprint),
            self._revision,
            tuple(definitions),
            fingerprint,
        )
        tools = {
            item.name: PinnedToolInvocation(
                item.semantic_identity,
                self._bound_invoke(item.name, self._revision),
            )
            for item in definitions
        }
        self._registry.pin(snapshot_id, self._revision, tools)
        key = (snapshot_id, self._revision)
        self._references[key] = 1
        return ToolBindingSnapshot(
            snapshot_id=snapshot_id,
            catalog=catalog,
            target_id=target.lease.target_id,
            capability_fingerprint=target.capability_fingerprint,
            provider_descriptor=f"runtime-tool-provider:{fingerprint}",
            registry_revision=self._revision,
            retention_lease_id=uuid4().hex,
        )

    async def dispatch(self, request: ToolDispatchRequest) -> ToolDispatchResult[ToolExecutionOutcome]:
        return await self._registry.dispatch(request)

    def release(self, snapshot: ToolBindingSnapshot) -> bool:
        key = (snapshot.snapshot_id, snapshot.registry_revision)
        references = self._references.get(key, 0)
        if references > 1:
            self._references[key] = references - 1
            return False
        self._references.pop(key, None)
        return self._registry.release(*key, references=0)

    def _definitions(self, *, include_hidden: bool) -> list[MaterializedToolDefinition]:
        if self._executor.command_protocol.value == "native":
            specs = self._executor.canonical_tool_specs(include_hidden=include_hidden)
        else:
            specs = [{"name": name, **schema} for name, schema in self._executor.all_xml_tool_schemas().items()]
        definitions = []
        for spec in specs:
            name = str(spec.get("name") or "")
            tool = self._executor._catalog.get(name)
            if not isinstance(tool, ExecutableToolBinding):
                raise TypeError(f"tool '{name}' is not an executable binding")
            compiled = tool.compiled_definition
            definitions.append(
                MaterializedToolDefinition(
                    name=name,
                    description=compiled.description,
                    input_schema=compiled.input_schema,
                    semantic_identity=compiled.semantic_identity,
                    effect=compiled.effect.value,
                    defer_loading=bool(spec.get("defer_loading")),
                )
            )
        return definitions

    def _bound_definition(self, name: str):
        tool = self._executor._catalog.get(name)
        if not isinstance(tool, ExecutableToolBinding):
            raise TypeError(f"tool '{name}' is not an executable binding")
        return tool.compiled_definition

    def _bound_invoke(self, name: str, registry_revision: int):
        pinned_tool = self._executor._catalog.get(name)
        if not isinstance(pinned_tool, ExecutableToolBinding):
            raise TypeError(f"tool '{name}' is not an executable binding")

        async def invoke(arguments):
            call_id = str(arguments.pop("__mote_call_id", "")) or None
            return await self._executor.run_pinned_command(
                pinned_tool,
                name,
                arguments,
                catalog_generation=registry_revision,
                result_id=call_id,
            )

        return invoke


__all__ = ["RuntimeToolSnapshotManager"]
