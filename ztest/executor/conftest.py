#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures and helpers for the ``metagpt.executor`` test suite.

Everything stays fully offline and deterministic — no LLM, no network, no MCP
servers. The building blocks are:

- A set of plain :class:`~metagpt.executor.base_tool.BaseTool` subclasses
  (``EchoTool`` / ``AddTool`` / ``FailTool`` / ``BoomTool`` / ``BgTool`` /
  ``MediaTool`` / ``CapTool``). They are NOT decorated with ``@register_tool``,
  so importing this module never pollutes the global tool registry.
- ``fresh_registry`` — an isolated :class:`ToolRegistry` built via ``__new__``
  (bypassing the ``Singleton`` metaclass) so ``register``/``get``/conflict
  tests never touch the process-wide singleton.
- ``restore_global_registry`` — snapshot/restore the real global registry +
  its ``_discovered`` flag for the few tests that must register into it.
- ``FakeRole`` — exposes a ``tool_capabilities()`` allowlist so ``bind()`` can
  inject narrow capabilities exactly like the real Role.
- ``make_executor`` — build a :class:`ToolExecutor` with no registry lookup and
  inject already-bound tool instances via ``register_tool_instance``.
"""
from __future__ import annotations

from typing import Any

import pytest

from metagpt.common.schema import ToolResultLimitConfig
from metagpt.executor.tasks.types import BgTaskResult
from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_executor import ToolExecutor
from metagpt.executor.tool_registry import ToolRegistry, registry as global_registry
from metagpt.executor.tool_result import ToolError, ToolResult


# ---------------------------------------------------------------------------
# Plain (unregistered) tools
# ---------------------------------------------------------------------------


class EchoTool(BaseTool):
    """Echo back the given text."""

    name = "Echo"
    aliases = ["echo", "Echo.run"]

    async def call(self, *, text: str) -> str:
        """Return *text* unchanged.

        Args:
            text: The text to echo back.
        """
        return text


class AddTool(BaseTool):
    """Add two integers."""

    name = "Add"

    async def call(self, *, a: int, b: int = 0) -> str:
        """Return the sum of *a* and *b*.

        Args:
            a: First addend.
            b: Second addend (optional).
        """
        return str(a + b)


class FailTool(BaseTool):
    """Always raises a (recoverable) ToolError."""

    name = "Fail"

    async def call(self, *, message: str = "bad args") -> str:
        raise ToolError(message)


class BoomTool(BaseTool):
    """Always raises an unexpected (non-ToolError) exception."""

    name = "Boom"

    async def call(self) -> str:
        raise RuntimeError("kaboom")


class BgTool(BaseTool):
    """Returns a BgTaskResult (background-capable tool)."""

    name = "Bg"

    async def call(self, *, label: str = "task") -> BgTaskResult:
        return BgTaskResult(result="started", command_name=label)


class MediaTool(BaseTool):
    """Returns a ToolResult carrying media (image)."""

    name = "Media"

    async def call(self, *, payload: str = "img") -> ToolResult:
        return ToolResult(output="Read image (1KB)", images=[payload])


class StructuredResultTool(BaseTool):
    """Returns a pre-built ToolResult (used as-is)."""

    name = "Struct"

    async def call(self, *, ok: bool = False) -> ToolResult:
        return ToolResult(output="structured", success=ok, data={"k": "v"})


class CapTool(BaseTool):
    """A tool that requires a Role capability named ``greet``."""

    name = "Cap"
    requires = ("greet",)

    async def call(self) -> str:
        # ``greet`` is injected by bind() from Role.tool_capabilities().
        return self.greet()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Fake Role
# ---------------------------------------------------------------------------


class FakeRole:
    """Minimal Role stand-in publishing an explicit capability allowlist."""

    def __init__(self, capabilities: dict[str, Any] | None = None) -> None:
        self._capabilities = capabilities if capabilities is not None else {"greet": lambda: "hi"}

    def tool_capabilities(self) -> dict[str, Any]:
        return self._capabilities


# ---------------------------------------------------------------------------
# Registry fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_registry() -> ToolRegistry:
    """An isolated ToolRegistry that bypasses the Singleton cache.

    ``ToolRegistry()`` would return the process-wide singleton; ``__new__``
    builds a brand-new instance whose ``_registry`` dict we initialise by hand.
    """
    reg = ToolRegistry.__new__(ToolRegistry)
    reg._registry = {}
    return reg


@pytest.fixture
def restore_global_registry():
    """Snapshot the real global registry and restore it after the test."""
    saved = dict(global_registry._registry)
    saved_discovered = ToolRegistry._discovered
    try:
        yield global_registry
    finally:
        global_registry._registry = saved
        ToolRegistry._discovered = saved_discovered


# ---------------------------------------------------------------------------
# Executor helper
# ---------------------------------------------------------------------------


def make_executor(
    *tools: BaseTool,
    session_id: str = "sess",
    role: FakeRole | None = None,
    limit_config: ToolResultLimitConfig | None = None,
    recovery_strategies: dict | None = None,
) -> ToolExecutor:
    """Build a ToolExecutor with no registry lookup and inject bound instances.

    Each tool is bound (session + optional role) and registered under all its
    names (primary + aliases), mirroring what the constructor does for static
    tools — but without touching the global registry.
    """
    ex = ToolExecutor(
        session_id,
        tools=None,
        limit_config=limit_config,
        recovery_strategies=recovery_strategies,
    )
    for tool in tools:
        tool.bind(session_id, role=role)
        names = [tool.name, *getattr(tool, "aliases", [])]
        ex.register_tool_instance(tool, names)
    return ex
