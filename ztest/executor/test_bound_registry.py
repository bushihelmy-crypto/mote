from __future__ import annotations

from mote.contracts.tool.catalog import ToolDispatchRequest
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.bound_registry import BoundToolRegistry, PinnedToolInvocation
from mote.runtime.tools.definitions import native_definition
from mote.runtime.tools.tool_binding import ExecutableToolBinding


class _ReadCapability(BaseTool):
    name = "Read"

    async def call(self, *, value: int) -> int:
        return value


def _pinned(*, catalog_generation: int) -> PinnedToolInvocation:
    capability = _ReadCapability()
    binding = ExecutableToolBinding(native_definition(_ReadCapability), capability)
    return PinnedToolInvocation(
        semantic_identity=binding.semantic_identity,
        canonical_name=binding.name,
        binding=binding,
        catalog_generation=catalog_generation,
    )


def test_duplicate_snapshot_revision_is_rejected():
    registry = BoundToolRegistry()
    registry.pin("snapshot", 1, {"Read": _pinned(catalog_generation=1)})
    try:
        registry.pin("snapshot", 1, {})
    except ValueError as exc:
        assert "already pinned" in str(exc)
    else:
        raise AssertionError("duplicate snapshot revision was accepted")


def test_resolve_requires_exact_pinned_revision():
    registry = BoundToolRegistry()
    registry.pin("snapshot", 2, {"Read": _pinned(catalog_generation=7)})
    stale, stale_conflict = registry.resolve(ToolDispatchRequest("snapshot", 1, "Read", {"value": 1}))
    current, current_conflict = registry.resolve(ToolDispatchRequest("snapshot", 2, "Read", {"value": 2}))

    assert stale is None
    assert stale_conflict == "unrecoverable_binding"
    assert current is not None
    assert current_conflict == ""
    assert current.canonical_name == "Read"
    assert current.catalog_generation == 7


def test_referenced_snapshot_cannot_be_released():
    registry = BoundToolRegistry()
    registry.pin("snapshot", 1, {})
    assert registry.release("snapshot", 1, references=1) is False
    assert registry.release("snapshot", 1, references=0) is True
