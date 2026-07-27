"""SplitToolMenuContextSource — the hint menu for client-deferred tools.

The client-side native path for a model without server-side ``defer_loading``
withholds each deferred tool's schema. This source carries only a compact
name-and-summary index so the model can choose what to reveal with SearchTools.

Lists ONLY the not-yet-revealed corpus tools (a brief one-line hint each + the
standing "search to load the full schema" note). Once a tool is revealed via
``SearchTools``, its complete definition is added to the native ``tools=``
projection on the next turn and it drops out of this menu.

Ephemeral (``save_to_context=False``): re-injected each turn into the request
tail, never persisted. Duck-typed (mirrors :class:`DeferredToolIndexContextSource`)
so the low ``context`` layer never imports the executor.
"""

from __future__ import annotations

from typing import Callable, Optional

from mote.contracts.ports import TurnContextPriority


class SplitToolMenuContextSource:
    """Emits one-line hints for client-side deferred native tools."""

    name = "split_tool_menu"
    # Same slot as the withhold-path menu: right after the tool catalogue, the
    # "what more can I do / how do I call it" surface. The two are mutually
    # exclusive (a role is on exactly one deferral path), so they never coexist.
    priority = TurnContextPriority.DEFERRED_TOOL_INDEX
    # Ephemeral: hints live after the cache breakpoint and are re-injected each
    # turn, so they are never persisted into history.
    save_to_context = False

    def __init__(self, get_menu: Callable[[], dict[str, str]]) -> None:
        self._get_menu = get_menu

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        menu = self._get_menu() if self._get_menu else {}
        if not menu:
            return None
        lines = [
            "# Additional tools (search to enable)",
            "These tools exist but are not loaded. Call SearchTools(query=...) "
            "with relevant keywords to reveal a tool; its complete description "
            "and parameter schema become available on the next turn.",
        ]
        # Sorted → stable ordering across turns.
        for tool_name in sorted(menu):
            lines.append(f"- {tool_name}: {menu[tool_name]}")
        return "\n".join(lines)


__all__ = ["SplitToolMenuContextSource"]
