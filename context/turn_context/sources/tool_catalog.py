"""ToolCatalogContextSource — the volatile tool catalog, per turn (XML mode).

The tool *catalog* (built-in / MCP / pipeline schemas) is kept out of the
cacheable system prompt: tools hot-reload (an MCP server connects, a pipeline
compiles, a built-in tool is gated on/off mid-session), and any change to the
catalog would bust the whole cached prompt prefix. So the volatile *list* is
delivered per-turn in the user prompt's ``<system-reminder>``, while the
*static usage guide* (how to call tools, that MCP names are ``server:tool``, that
MCP may fail — constant per session) stays in the system prompt as the channel's
``${tool_usage_guide}``.

This is the XML-protocol path only. Under provider-native tool-use the catalog
rides the API ``tools=`` param, so the channel reports ``wants_tool_catalog()``
False and this source self-suppresses (renders ``None``).

The first turn emits the full catalog; every later turn emits only the
*newly-appeared* tools (tracked by ``_sent_names``). Removals are not
re-announced (the model simply stops seeing them; a stale attempt fails cleanly).

Push→pull bridge in one object (like ``CompactionNoticeContextSource``):
- as an :class:`~mote.common.interface.ObservationSubscriber` it catches any
  :data:`~mote.common.events.HISTORY_RESET_EVENTS` (a compaction *or* a ``/clear`` /
  user delete that structurally rebuilds stored history) off the bus and resets
  the incremental frontier, so the next turn re-sends the *full* catalog (the
  earlier full listing was condensed away by the compaction or pruned by the
  edit); it also catches :class:`~mote.common.events.ToolsChangedEvent` and drops
  the vanished names from the frontier, so a tool that is de-registered and later
  re-registered is re-announced (rather than silently withheld because its name
  still sat in ``_sent_names``);
- as an :class:`~mote.common.interface.EphemeralContextSource` it renders the
  catalog once per think() cycle.

Duck-typed (mirrors :class:`SkillListingContextSource`): it holds callables so
the low ``context`` layer never imports the executor or the command channel.
"""

from __future__ import annotations

import json
from typing import Callable, Optional, Protocol

from mote.common.events import HISTORY_RESET_EVENTS, ToolsChangedEvent
from mote.common.interface import ObservationSubscriber, TurnContextPriority


class _CatalogExecutor(Protocol):
    """The tool-schema slice this source reads off the executor (duck-typed).

    Structural only — keeps the low ``context`` layer from importing the executor;
    any object exposing these three schema getters satisfies it.
    """

    def get_tool_schemas(self) -> Optional[dict]:
        ...

    def get_mcp_tool_schemas(self) -> Optional[dict]:
        ...

    def get_pipeline_tool_schemas(self) -> Optional[dict]:
        ...


class _CatalogChannel(Protocol):
    """The command-channel slice this source consults (duck-typed)."""

    def wants_tool_catalog(self) -> bool:
        ...


class ToolCatalogContextSource(ObservationSubscriber):
    """Emits the volatile tool catalog per turn (XML mode), incrementally."""

    name = "tool_catalog"
    # Leads the reminder block (lowest priority renders first): the tool catalog
    # is the most fundamental surface — what the model can *do* this turn. The
    # same value serves as the ObservationSubscriber dispatch priority, where it
    # is immaterial (this handler only observes — it returns no outcome).
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

    async def handle(self, event) -> None:
        """Keep the incremental frontier consistent with catalog-shaping events.

        Any ``HISTORY_RESET_EVENTS`` resets it wholesale — a compaction condensed
        the prior full listing away, or a ``/clear`` / user delete pruned the
        messages that carried it; either way re-send the whole catalog. A
        ``ToolsChangedEvent`` (checked first, since it is not a history-reset
        event) drops only the removed names, so a later re-registration of the
        same name is treated as new and re-announced. All other events ignored.
        """
        if isinstance(event, ToolsChangedEvent):
            self._sent_names -= set(event.removed)
        elif isinstance(event, HISTORY_RESET_EVENTS):
            self._sent_names = set()
        return None

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        channel = self._get_channel() if self._get_channel else None
        # Native tool-use passes tools via the API ``tools=`` param; the system
        # prompt / reminder must NOT re-describe them. Self-suppress.
        if channel is None or not channel.wants_tool_catalog():
            return None

        executor = self._get_executor() if self._get_executor else None
        if executor is None:
            return None

        builtin = executor.get_tool_schemas() or {}
        # MCP master switch off → drop the whole MCP block, so the reminder never
        # lists MCP tools even if adapters happen to be bound.
        if self._mcp_enabled is not None and not self._mcp_enabled():
            mcp = {}
        else:
            mcp = executor.get_mcp_tool_schemas() or {}
        pipeline = executor.get_pipeline_tool_schemas() or {}

        current = set(builtin) | set(mcp) | set(pipeline)
        if not current:
            return None

        first_turn = not self._sent_names
        if first_turn:
            self._sent_names = set(current)
            return _render_full(builtin, mcp, pipeline)

        # Incremental: only tools not yet announced.
        new_names = current - self._sent_names
        if not new_names:
            return None
        self._sent_names |= new_names
        return _render_delta(builtin, mcp, pipeline, new_names)


def _render_full(builtin: dict, mcp: dict, pipeline: dict) -> str:
    """The whole catalog, one JSON block per category (the usage guide lives in
    the system prompt as ``${tool_usage_guide}`` — not repeated here)."""
    blocks: list[str] = []
    if builtin:
        blocks.append(f"# Available Commands\n{json.dumps(builtin, ensure_ascii=False)}")
    if mcp:
        blocks.append(f"# MCP Tools\n{json.dumps(mcp, ensure_ascii=False)}")
    if pipeline:
        blocks.append(f"# Pipeline Tools\n{json.dumps(pipeline, ensure_ascii=False)}")
    return "\n\n".join(blocks)


def _render_delta(builtin: dict, mcp: dict, pipeline: dict, new_names: set[str]) -> str:
    """Only the newly-appeared tools, filtered per category, under a delta header."""
    blocks: list[str] = ["# New tools available"]
    new_builtin = {k: v for k, v in builtin.items() if k in new_names}
    new_mcp = {k: v for k, v in mcp.items() if k in new_names}
    new_pipeline = {k: v for k, v in pipeline.items() if k in new_names}
    if new_builtin:
        blocks.append(f"# Available Commands\n{json.dumps(new_builtin, ensure_ascii=False)}")
    if new_mcp:
        blocks.append(f"# MCP Tools\n{json.dumps(new_mcp, ensure_ascii=False)}")
    if new_pipeline:
        blocks.append(f"# Pipeline Tools\n{json.dumps(new_pipeline, ensure_ascii=False)}")
    return "\n\n".join(blocks)


__all__ = ["ToolCatalogContextSource"]
