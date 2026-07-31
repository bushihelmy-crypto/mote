from __future__ import annotations

import pytest

from mote.contracts.tool.catalog import ToolDispatchRequest
from mote.runtime.tools.bound_registry import BoundTool, BoundToolRegistry


async def _invoke(arguments):
    return arguments["value"]


def test_duplicate_snapshot_revision_is_rejected():
    registry = BoundToolRegistry()
    registry.pin("snapshot", 1, {"Read": BoundTool("read@1", _invoke)})
    with pytest.raises(ValueError, match="already pinned"):
        registry.pin("snapshot", 1, {})


@pytest.mark.asyncio
async def test_dispatch_requires_exact_pinned_revision():
    registry = BoundToolRegistry()
    registry.pin("snapshot", 2, {"Read": BoundTool("read@2", _invoke)})
    stale = await registry.dispatch(ToolDispatchRequest("snapshot", 1, "Read", {"value": 1}))
    current = await registry.dispatch(ToolDispatchRequest("snapshot", 2, "Read", {"value": 2}))

    assert stale.conflict == "unrecoverable_binding"
    assert current.value == 2


def test_referenced_snapshot_cannot_be_released():
    registry = BoundToolRegistry()
    registry.pin("snapshot", 1, {})
    assert registry.release("snapshot", 1, references=1) is False
    assert registry.release("snapshot", 1, references=0) is True
