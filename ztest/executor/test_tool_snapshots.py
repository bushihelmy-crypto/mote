from __future__ import annotations

from types import SimpleNamespace

import pytest

from mote.contracts.tool import ToolEffect
from mote.contracts.tool.catalog import ToolDispatchRequest
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.definitions import native_definition
from mote.runtime.tools.snapshots import RuntimeToolSnapshotManager
from mote.runtime.tools.tool_binding import ExecutableToolBinding


class ReadCapability(BaseTool):
    name = "Read"
    effect = ToolEffect.PURE

    async def call(self, *, path: str = ""):
        """Read one path.

        Args:
            path: path to read.
        """

        return path


def binding():
    capability = ReadCapability()
    return ExecutableToolBinding(native_definition(ReadCapability), capability)


class Catalog:
    def __init__(self, tool):
        self.tool = tool

    def get(self, name):
        return self.tool if name == "Read" else None


class Executor:
    command_protocol = SimpleNamespace(value="native")

    def __init__(self):
        self.tool = binding()
        self._catalog = Catalog(self.tool)
        self.calls = []

    def canonical_tool_specs(self, *, include_hidden):
        return [
            {
                "name": "Read",
                "description": "read",
                "input_schema": {"type": "object"},
            }
        ]

    async def run_command(self, name, arguments, *, result_id=None):
        self.calls.append((name, arguments, result_id))
        return "ok"

    async def run_pinned_command(self, binding, name, arguments, *, catalog_generation, result_id=None):
        self.calls.append((name, arguments, result_id))
        return await binding.call(**arguments)


def target():
    return SimpleNamespace(
        lease=SimpleNamespace(target_id="endpoint"),
        capability_fingerprint="capabilities",
    )


@pytest.mark.asyncio
async def test_snapshot_dispatch_is_revision_pinned():
    executor = Executor()
    manager = RuntimeToolSnapshotManager(executor)
    snapshot = manager.materialize(target(), include_hidden=False)
    result = await manager.dispatch(
        ToolDispatchRequest(
            snapshot.snapshot_id,
            snapshot.registry_revision,
            "Read",
            {"path": "a"},
            "call-1",
        )
    )

    assert result.success is True
    assert executor.calls == [("Read", {"path": "a"}, "call-1")]


@pytest.mark.asyncio
async def test_replaced_capability_does_not_rebind_old_snapshot_by_name():
    executor = Executor()
    manager = RuntimeToolSnapshotManager(executor)
    snapshot = manager.materialize(target(), include_hidden=False)
    executor._catalog.tool = binding()

    result = await manager.dispatch(
        ToolDispatchRequest(
            snapshot.snapshot_id,
            snapshot.registry_revision,
            "Read",
            {},
        )
    )

    assert result.success is True
    assert executor.calls == [("Read", {}, None)]


@pytest.mark.asyncio
async def test_released_snapshot_cannot_dispatch():
    executor = Executor()
    manager = RuntimeToolSnapshotManager(executor)
    snapshot = manager.materialize(target(), include_hidden=False)
    assert manager.release(snapshot) is True

    result = await manager.dispatch(
        ToolDispatchRequest(
            snapshot.snapshot_id,
            snapshot.registry_revision,
            "Read",
            {},
        )
    )
    assert result.conflict == "unrecoverable_binding"
