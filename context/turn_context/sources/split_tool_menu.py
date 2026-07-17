"""SplitToolMenuContextSource — the description home for split-mode deferred tools.

The client-side SPLIT native path (an incapable native model: no server-side
``defer_loading``) keeps each deferred (corpus) tool's NAME + ``input_schema`` on
the ``tools=`` wire with only a stub description, so the tool stays fully
callable and the ``tools=`` prefix is byte-stable (prompt cache preserved). This
source carries the PROSE that was stripped off the wire — injected into the
ephemeral reminder tail (after the cache breakpoint) so it never churns the
cached prefix.

Lists ONLY the not-yet-revealed corpus tools (a brief one-line hint each + the
standing "search to load the full description" note). Once a tool is revealed
via ``SearchTools`` its FULL description is *persisted* into the conversation
(the SearchTools result body, also registered as a sticky resource so it
survives compaction) — so it enters the cached prefix, paid once, instead of
riding this uncached reminder tail every turn. A revealed tool therefore drops
OUT of this menu; the ephemeral surface only ever shrinks.

Ephemeral (``save_to_context=False``): re-injected each turn into the request
tail, never persisted. Duck-typed (mirrors :class:`DeferredToolIndexContextSource`)
so the low ``context`` layer never imports the executor.
"""

from __future__ import annotations

from typing import Callable, Optional

from mote.common.interface import TurnContextPriority


class SplitToolMenuContextSource:
    """Emits split-mode deferred tools' descriptions (brief→full on reveal)."""

    name = "split_tool_menu"
    # Same slot as the withhold-path menu: right after the tool catalogue, the
    # "what more can I do / how do I call it" surface. The two are mutually
    # exclusive (a role is on exactly one deferral path), so they never coexist.
    priority = TurnContextPriority.DEFERRED_TOOL_INDEX
    # Ephemeral: the descriptions live after the cache breakpoint and are
    # re-injected each turn, so they are never persisted into history.
    save_to_context = False

    def __init__(self, get_menu: Callable[[], dict[str, str]]) -> None:
        self._get_menu = get_menu

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        menu = self._get_menu() if self._get_menu else {}
        if not menu:
            return None
        lines = [
            "# Additional tools",
            "These tools are callable (their name + parameters are in your tool "
            "list) but only a one-line hint is shown here to keep the prompt "
            "compact. Call SearchTools(query=...) with keywords to load a tool's "
            "FULL description into the conversation before relying on it.",
        ]
        # Sorted → stable ordering across turns.
        for tool_name in sorted(menu):
            lines.append(f"- {tool_name}: {menu[tool_name]}")
        return "\n".join(lines)


__all__ = ["SplitToolMenuContextSource"]
