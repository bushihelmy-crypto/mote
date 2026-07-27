"""ToolCatalogContextSource — volatile tool definitions, delivered per turn.

MCP and pipeline definitions are hot-reloadable, so their volatile catalog is
delivered in the user prompt's ``<system-reminder>``. XML built-in definitions
are not emitted here; they live in the system prompt's ``# Available Commands``
section. The static usage guide stays in the system prompt as the channel's
``${tool_usage_guide}``.

Both XML and Native receive every MCP definition here. XML additionally receives
hot-reloadable pipeline definitions. Native built-ins/pipelines use the provider
``tools=`` channel; MCP definitions remain ordinary Native definitions on that
wire and are also announced here for dynamic discovery. There is no separate
``NativeMcpSpec`` abstraction.

The first turn emits the full catalog; every later turn emits only the
*newly-appeared* tools (tracked by ``_sent_names``). Removals are not
re-announced (the model simply stops seeing them; a stale attempt fails cleanly).

After a durable model-context rebuild commits, the context domain calls
``on_model_context_rebuilt`` directly before publishing the new live view. That
resets the incremental frontier, so the next turn re-sends the full catalog.
Tool removals are reconciled from the live catalog during every render; no
correctness state depends on lossy telemetry.

Duck-typed (mirrors :class:`SkillListingContextSource`): it holds callables so
the low ``context`` layer never imports the executor or the command channel.
"""

from __future__ import annotations

import json
from typing import Callable, Optional, Protocol

from mote.contracts.ports import TurnContextPriority
from mote.runtime.events import MODEL_CONTEXT_REBUILT_EVENTS


class _CatalogExecutor(Protocol):
    """The tool-schema slice this source reads off the executor (duck-typed).

    Structural only — keeps the low ``context`` layer from importing the executor;
    any object exposing these two dynamic-schema getters satisfies it.
    """

    def mcp_tool_schemas(self) -> Optional[dict]:
        ...

    def xml_pipeline_tool_schemas(self) -> Optional[dict]:
        ...


class _CatalogChannel(Protocol):
    """The command-channel slice this source consults (duck-typed)."""

    def wants_tool_catalog(self) -> bool:
        ...


class ToolCatalogContextSource:
    """Emits volatile protocol definitions per turn, incrementally."""

    name = "tool_catalog"
    # Leads the reminder block (lowest priority renders first): the tool catalog
    # is the most fundamental surface — what the model can *do* this turn.
    priority = TurnContextPriority.TOOL_CATALOG
    save_to_context = True

    def __init__(
        self,
        get_executor: Callable[[], Optional[_CatalogExecutor]],
        get_channel: Callable[[], Optional[_CatalogChannel]],
        mcp_enabled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._get_executor = get_executor
        self._get_channel = get_channel
        # Optional MCP master-switch gate (``config.mcp.enabled``). When supplied
        # and False, MCP schemas are dropped from the catalog — on top of the
        # original logic (an empty MCP map self-suppresses that block anyway).
        # None → gate on the original logic alone (backwards-compatible).
        self._mcp_enabled = mcp_enabled
        # Names already surfaced in a prior turn — the incremental frontier.
        self._sent_names: set[str] = set()

    async def on_model_context_rebuilt(self, event: object) -> None:
        """Reset the incremental frontier after a committed context rebuild."""

        if isinstance(event, MODEL_CONTEXT_REBUILT_EVENTS):
            self._sent_names = set()

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        channel = self._get_channel() if self._get_channel else None
        if channel is None:
            return None

        executor = self._get_executor() if self._get_executor else None
        if executor is None:
            return None

        # XML built-ins are rendered into the system prompt. Native built-ins and
        # pipelines ride ``tools=``. MCP is hot-loadable under both protocols and
        # therefore always uses this reminder surface.
        wants_catalog = channel.wants_tool_catalog()
        # MCP master switch off → drop the whole MCP block, so the reminder never
        # lists MCP tools even if adapters happen to be bound.
        if self._mcp_enabled is not None and not self._mcp_enabled():
            mcp = {}
        else:
            mcp = executor.mcp_tool_schemas() or {}
        pipeline = executor.xml_pipeline_tool_schemas() or {} if wants_catalog else {}

        current = set(mcp) | set(pipeline)
        self._sent_names.intersection_update(current)
        if not current:
            return None

        first_turn = not self._sent_names
        if first_turn:
            self._sent_names = set(current)
            return _render_full(mcp, pipeline)

        # Incremental: only tools not yet announced.
        new_names = current - self._sent_names
        if not new_names:
            return None
        self._sent_names |= new_names
        return _render_delta(mcp, pipeline, new_names)


def _render_full(mcp: dict, pipeline: dict) -> str:
    """The dynamic catalog; static XML built-ins are already in the SP."""
    blocks: list[str] = []
    if mcp:
        blocks.append(f"# MCP Tools\n{json.dumps(mcp, ensure_ascii=False, sort_keys=True)}")
    if pipeline:
        blocks.append(f"# Pipeline Tools\n{json.dumps(pipeline, ensure_ascii=False, sort_keys=True)}")
    return "\n\n".join(blocks)


def _render_delta(mcp: dict, pipeline: dict, new_names: set[str]) -> str:
    """Only the newly-appeared tools, filtered per category, under a delta header."""
    blocks: list[str] = ["# New tools available"]
    new_mcp = {k: v for k, v in mcp.items() if k in new_names}
    new_pipeline = {k: v for k, v in pipeline.items() if k in new_names}
    if new_mcp:
        blocks.append(f"# MCP Tools\n{json.dumps(new_mcp, ensure_ascii=False, sort_keys=True)}")
    if new_pipeline:
        blocks.append(f"# Pipeline Tools\n{json.dumps(new_pipeline, ensure_ascii=False, sort_keys=True)}")
    return "\n\n".join(blocks)


__all__ = ["ToolCatalogContextSource"]
