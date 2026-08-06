"""Materialization and pinned dispatch for run-scoped tool snapshots."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from mote.contracts.execution.pending_act import ToolCompositionDefinitionRef
from mote.contracts.ports.tool.approval import ToolApprovalCoordinator
from mote.contracts.tool.catalog import (
    MaterializedToolCatalog,
    MaterializedToolDefinition,
    ToolBindingSnapshot,
    ToolCatalogIdentity,
    ToolDispatchRequest,
    ToolDispatchResult,
    ToolExecutionOutcome,
)
from mote.contracts.tool.identity import ToolInvocationIdentity
from mote.runtime.tools.bound_registry import BoundToolRegistry, PinnedToolInvocation
from mote.runtime.tools.definition_compiler import compile_tool_catalog_identity
from mote.runtime.tools.tool_binding import ExecutableToolBinding
from mote.runtime.tools.tool_pipeline import ToolExecution


class RuntimeToolSnapshotManager:
    def __init__(self, executor, *, composition_generation_id: str) -> None:
        if not composition_generation_id:
            raise ValueError("tool snapshots require an approved composition generation")
        self._executor = executor
        self._composition_generation_id = composition_generation_id
        self._registry = BoundToolRegistry()
        self._revision = 0
        self._references: dict[tuple[str, int], int] = {}
        self._authorized: dict[tuple[str, int, str], ToolExecution] = {}

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
                item.name,
                self._bound_binding(item.name),
                self._executor.tool_binding_generation,
            )
            for item in definitions
        }
        self._registry.pin(snapshot_id, self._revision, tools)
        key = (snapshot_id, self._revision)
        self._references[key] = 1
        return ToolBindingSnapshot(
            snapshot_id=snapshot_id,
            composition_generation_id=self._composition_generation_id,
            catalog=catalog,
            target_id=target.lease.target_id,
            capability_fingerprint=target.capability_fingerprint,
            provider_descriptor=f"runtime-tool-provider:{fingerprint}",
            registry_revision=self._revision,
            retention_lease_id=uuid4().hex,
        )

    def restore(
        self,
        definition: ToolCompositionDefinitionRef,
        target,
        *,
        include_hidden: bool,
    ) -> ToolBindingSnapshot:
        """Pin a live candidate only when every durable definition fact matches."""

        snapshot = self.materialize(target, include_hidden=include_hidden)
        provider_digest = f"sha256-{hashlib.sha256(snapshot.provider_descriptor.encode('utf-8')).hexdigest()}"
        if (
            snapshot.catalog.identity.catalog_id != definition.blueprint_identity
            or snapshot.catalog.identity.version != definition.blueprint_version
            or snapshot.catalog.fingerprint != definition.executable_digest
            or snapshot.composition_generation_id != definition.composition_generation_id
            or snapshot.catalog.fingerprint != definition.catalog_fingerprint
            or provider_digest != definition.provider_descriptor_digest
            or snapshot.composition_generation_id != definition.policy_generation
            or snapshot.capability_fingerprint != definition.capability_fingerprint
        ):
            self.release(snapshot)
            raise ValueError("recovered PendingAct tool composition cannot be reconstructed exactly")
        return snapshot

    async def dispatch(self, request: ToolDispatchRequest) -> ToolDispatchResult[ToolExecutionOutcome]:
        execution = self._authorized.pop(_request_key(request), None)
        if execution is None:
            return ToolDispatchResult(False, conflict="tool invocation was not authorized")
        value = await self._executor.invoke_authorized_pinned_command(execution)
        return ToolDispatchResult(True, value=value)

    async def authorize(self, request: ToolDispatchRequest) -> ToolDispatchResult[None]:
        pinned, conflict = self._registry.resolve(request)
        if pinned is None:
            return ToolDispatchResult(False, conflict=conflict)
        key = _request_key(request)
        if key in self._authorized:
            return ToolDispatchResult(False, conflict="tool invocation is already authorized")
        identity = self.invocation_identity(request)
        execution, rejected = await self._executor.authorize_pinned_command(
            pinned.binding,
            pinned.canonical_name,
            dict(request.arguments),
            identity,
        )
        if rejected is not None:
            return ToolDispatchResult(
                False,
                conflict=rejected.output,
                approval_request_id=execution.approval_request_id,
            )
        self._authorized[key] = execution
        return ToolDispatchResult(True)

    def bind_approval_coordinator(self, coordinator: ToolApprovalCoordinator | None) -> None:
        self._executor.bind_approval_coordinator(coordinator)

    def bind_fileops_transaction(self, request: ToolDispatchRequest, transaction_id: str | None) -> None:
        execution = self._authorized.get(_request_key(request))
        if execution is None:
            raise ValueError("file transaction binding requires an authorized invocation")
        if transaction_id is not None and not transaction_id:
            raise ValueError("file transaction identity must be non-empty")
        execution.fileops_transaction_id = transaction_id

    def invocation_identity(self, request: ToolDispatchRequest) -> ToolInvocationIdentity:
        authorized = self._authorized.get(_request_key(request))
        if authorized is not None:
            return authorized.identity
        pinned, conflict = self._registry.resolve(request)
        if pinned is None:
            raise ValueError(conflict)
        return self._executor.next_invocation_identity(
            pinned.binding,
            request.arguments,
            catalog_generation=pinned.catalog_generation,
            result_id=request.call_id,
        )

    def release(self, snapshot: ToolBindingSnapshot) -> bool:
        key = (snapshot.snapshot_id, snapshot.registry_revision)
        references = self._references.get(key, 0)
        if references > 1:
            self._references[key] = references - 1
            return False
        self._references.pop(key, None)
        self._authorized = {
            request_key: execution for request_key, execution in self._authorized.items() if request_key[:2] != key
        }
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
                    input_schema=dict(compiled.input_schema),
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

    def _bound_binding(self, name: str) -> ExecutableToolBinding:
        pinned_tool = self._executor._catalog.get(name)
        if not isinstance(pinned_tool, ExecutableToolBinding):
            raise TypeError(f"tool '{name}' is not an executable binding")

        return pinned_tool


__all__ = ["RuntimeToolSnapshotManager"]


def _request_key(request: ToolDispatchRequest) -> tuple[str, int, str]:
    return request.snapshot_id, request.registry_revision, request.call_id
