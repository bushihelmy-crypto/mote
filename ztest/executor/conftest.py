#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures and helpers for the ``mote.runtime.tools`` test suite.

Everything stays fully offline and deterministic — no LLM, no network, no MCP
servers. The building blocks are:

- A set of plain :class:`~mote.runtime.tools.base_tool.BaseTool` subclasses
  (``EchoTool`` / ``AddTool`` / ``FailTool`` / ``BoomTool`` / ``BgTool`` /
  ``MediaTool`` / ``CapTool``). Importing this module has no discovery side
  effects.
- ``fresh_catalog`` - an immutable isolated :class:`ToolCatalog`.
- ``FakeRole`` — exposes a ``tool_capabilities()`` allowlist so ``bind()`` can
  inject narrow capabilities exactly like the real Role.
- ``make_executor`` — build a :class:`ToolExecutor` with no registry lookup and
  inject already-bound tool instances via ``register_tool_instance``.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mote.contracts.config.tool import DurableConfig, RunJournalConfig, ToolResultLimitConfig
from mote.contracts.tool.errors import ToolError
from mote.contracts.tool.execution import ToolExecutionKind
from mote.orchestration.background_tasks.model import BgTaskResult
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.definitions import native_definition
from mote.runtime.tools.provider import NativeToolset
from mote.runtime.tools.tool_executor import ToolExecutor
from mote.runtime.tools.tool_registry import ToolCatalog
from mote.runtime.tools.tool_result import ToolMedia, ToolResult
from mote.ztest.artifact_fakes import artifact_media


@pytest.fixture(autouse=True)
def _inject_executor_workspace(tmp_path, monkeypatch):
    original_init = ToolExecutor.__init__
    workspace = SessionWorkspace(tmp_path / "workspace")

    def init_with_workspace(self, *args, **kwargs):
        kwargs.setdefault("workspace_store", workspace)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(ToolExecutor, "__init__", init_with_workspace)


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
    execution_kind = ToolExecutionKind.WORKFLOW_DEFERRED

    async def call(self, *, label: str = "task") -> BgTaskResult:
        return BgTaskResult.foreground("started", command_name=label)


class MediaTool(BaseTool):
    """Returns a ToolResult carrying media (image)."""

    name = "Media"

    async def call(self, *, payload: str = "img") -> ToolResult:
        return ToolResult(output="Read image (1KB)", media=[artifact_media("image", payload)])


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
def fresh_catalog() -> ToolCatalog:
    """Return an immutable catalog with no hidden process state."""
    return ToolCatalog.from_types(())


# ---------------------------------------------------------------------------
# Executor helper
# ---------------------------------------------------------------------------


def make_executor(
    *tools: BaseTool,
    session_id: str = "sess",
    role: FakeRole | None = None,
    limit_config: ToolResultLimitConfig | None = None,
    journal_config: RunJournalConfig | None = None,
    durable_config: DurableConfig | None = None,
    recovery_strategies: dict | None = None,
    workspace_store=None,
) -> ToolExecutor:
    """Build a ToolExecutor with no registry lookup and inject bound instances.

    Each tool is bound (session + optional role) and registered under all its
    names (primary + aliases), mirroring what the constructor does for static
    tools — but without touching the global registry.
    """
    definitions = []
    declared = []
    for tool in tools:
        definition = native_definition(type(tool))
        definitions.append(replace(definition, capability_factory=lambda tool=tool: tool))
        declared.append(tool.name)
    ex = ToolExecutor(
        session_id,
        tools=declared,
        role=role,
        limit_config=limit_config,
        journal_config=journal_config,
        durable_config=durable_config,
        recovery_strategies=recovery_strategies,
        workspace_store=workspace_store or SessionWorkspace(Path(tempfile.mkdtemp(prefix="mote-tool-test-"))),
        toolsets=(NativeToolset("test.native", definitions),),
        command_protocol="native",
    )
    return ex
