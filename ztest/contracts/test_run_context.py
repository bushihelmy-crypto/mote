from __future__ import annotations

from dataclasses import dataclass

import pytest

from mote.kernel.execution.run_context import RunContext, ToolContext


@dataclass(frozen=True)
class AppDependencies:
    database: object
    secret: str


@dataclass(frozen=True)
class ReadOnlyToolDependencies:
    database: object


def test_tool_context_requires_an_explicit_narrowing_projection() -> None:
    database = object()
    dependencies = AppDependencies(database=database, secret="agent-only")
    run_context = RunContext(
        deps=dependencies,
        session_id="session",
        run_id="run",
    )

    tool_context = run_context.for_tool(
        lambda deps: ReadOnlyToolDependencies(database=deps.database),
        tool_call_id="call",
    )

    assert isinstance(tool_context, ToolContext)
    assert tool_context.deps == ReadOnlyToolDependencies(database=database)
    assert not hasattr(tool_context.deps, "secret")
    assert tool_context.session_id == "session"
    assert tool_context.run_id == "run"
    assert tool_context.tool_call_id == "call"


def test_context_values_preserve_dependency_identity_and_freeze_metadata() -> None:
    dependencies = object()
    source = {"tenant": "one"}
    run_context = RunContext(
        deps=dependencies,
        session_id="session",
        run_id="run",
        metadata=source,
    )
    source["tenant"] = "two"

    assert run_context.deps is dependencies
    assert run_context.metadata == {"tenant": "one"}
    with pytest.raises(TypeError):
        run_context.metadata["tenant"] = "three"  # type: ignore[index]

    tool_context = run_context.for_tool(lambda deps: deps)
    with pytest.raises(TypeError):
        tool_context.metadata["tenant"] = "three"  # type: ignore[index]
