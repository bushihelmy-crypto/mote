#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""McpLifecycle — owns the executor's live MCP manager slot.

Holds the one :class:`UniversalMCP` an executor may have connected and the two
low-level transitions over it: :meth:`bind` (construct → initialize → register
discovered tools onto a registrar) and :meth:`teardown` (close its clients and
forget it). Both :meth:`ToolExecutor.init_mcp` and :meth:`ToolExecutor.reload_mcp`
drive these, so the "new UniversalMCP + initialize + register_tools" sequence
lives in exactly one place.

The orchestration around a reload — tearing the old adapters out of the tool
catalog, reclaiming their sessions, and announcing the churn on the bus — stays
in the executor, which owns the catalog / session / bus. This object owns only
the manager slot.
"""
from __future__ import annotations

from typing import Any

from mote.executor.mcp.universal import UniversalMCP


class McpLifecycle:
    """The executor's MCP manager slot + its construct/teardown transitions."""

    def __init__(self) -> None:
        self._mcp: UniversalMCP | None = None

    @property
    def mcp(self) -> UniversalMCP | None:
        """The connected manager, or None when no MCP is bound."""
        return self._mcp

    @property
    def active(self) -> bool:
        """True once a manager is connected (used by init_mcp's idempotence guard)."""
        return self._mcp is not None

    async def bind(self, mcps: list[str] | None, registrar: Any) -> None:
        """Construct a fresh manager, initialize it, register its tools.

        *mcps* narrows initialization to those server names; ``None`` loads every
        server in ``mcp_config.json`` (the master-switch path). *registrar* is
        any object exposing ``register_tool_instance`` (the executor);
        ``UniversalMCP.register_tools`` wraps each discovered tool in an adapter
        and registers it there. Replaces any current manager slot — callers that
        must not clobber a live one guard with :attr:`active`, and a reload tears
        the old one down via :meth:`teardown` first.
        """
        self._mcp = UniversalMCP()
        await self._mcp.initialize(server_names=mcps)
        self._mcp.register_tools(registrar)

    async def teardown(self) -> None:
        """Close the current manager's clients and clear the slot (no-op if empty)."""
        if self._mcp is not None:
            await self._mcp.cleanup_clients()
            self._mcp = None
