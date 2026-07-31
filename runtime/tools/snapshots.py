"""Materialization and pinned dispatch for run-scoped tool snapshots."""

from __future__ import annotations

import hashlib
import json
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
from mote.runtime.tools.bound_registry import BoundTool, BoundToolRegistry, UnrecoverableBindingError


class RuntimeToolSnapshotManager:
    def __init__(self, executor) -> None:
        self._executor = executor
        self._registry = BoundToolRegistry()
        self._revision = 0
        self._references: dict[tuple[str, int], int] = {}

    def materialize(self, target, *, include_hidden: bool) -> ToolBindingSnapshot:
        definitions = self._definitions(include_hidden=include_hidden)
        payload = [
            {
                "name": item.name,
                "description": item.description,
                "input_schema": item.input_schema,
                "semantic_identity": item.semantic_identity,
                "effect": item.effect,
                "defer_loading": item.defer_loading,
            }
            for item in definitions
        ]
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self._revision += 1
        snapshot_id = uuid4().hex
        catalog = MaterializedToolCatalog(
            ToolCatalogIdentity("runtime-tools", "1"),
            self._revision,
            tuple(definitions),
            fingerprint,
        )
        tools = {
            item.name: BoundTool(
                item.semantic_identity,
                self._bound_invoke(item.name),
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
            provider_descriptor="runtime-tool-provider@1",
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
            effect = getattr(getattr(tool, "effect", None), "value", None) or str(getattr(tool, "effect", "pure"))
            definitions.append(
                MaterializedToolDefinition(
                    name=name,
                    description=str(spec.get("description") or ""),
                    input_schema=dict(
                        spec.get("input_schema") or spec.get("parameters") or {"type": "object", "properties": {}}
                    ),
                    semantic_identity=f"{name}@{getattr(tool, 'definition_version', '1')}",
                    effect=effect,
                    defer_loading=bool(spec.get("defer_loading")),
                )
            )
        return definitions

    def _bound_invoke(self, name: str):
        pinned_tool = self._executor._catalog.get(name)

        async def invoke(arguments):
            if self._executor._catalog.get(name) is not pinned_tool:
                raise UnrecoverableBindingError(name)
            call_id = str(arguments.pop("__mote_call_id", "")) or None
            return await self._executor.run_command(name, arguments, result_id=call_id)

        return invoke


__all__ = ["RuntimeToolSnapshotManager"]
