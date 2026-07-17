"""DeferredToolIndexContextSource — the searchable menu of deferred tools.

Tool-search's *always-on compact index*: when a role defers peripheral tools
(``RoleSchema.deferred_tools``), their full schema is withheld from both
channels; this source renders a small listing (name + one-line description, NO
parameters) of the still-hidden deferred tools every turn, so the model knows
what it can discover and can ``SearchTools(query=...)`` to reveal a tool's full
schema.

Reveal-aware (built with ``include_revealed=False``): once a tool is revealed its
full schema is already on the active channel, so it DROPS out of this "search to
enable" menu — keeping it would mislead the model and waste tokens. The menu
therefore only ever shrinks as tools are revealed. This costs no prompt-cache
churn: the source is ephemeral (``save_to_context=False``) and rides the reminder
tail AFTER the cache breakpoint, re-injected each turn and never in the cached
prefix (nor persisted into history). Mirrors :class:`SplitToolMenuContextSource`,
which likewise lists only the unrevealed.

Duck-typed (mirrors :class:`ToolCatalogContextSource`): it holds a single
callable so the low ``context`` layer never imports the executor.
"""

from __future__ import annotations

from typing import Callable, Optional

from mote.common.interface import TurnContextPriority


class DeferredToolIndexContextSource:
    """Emits the stable searchable menu of deferred (hidden) tools, per turn."""

    name = "deferred_tool_index"
    # Right after the tool catalogue: the deferred menu is the second-most
    # fundamental "what can I do" surface — what the model can *unlock*.
    priority = TurnContextPriority.DEFERRED_TOOL_INDEX
    # Ephemeral: the menu is byte-stable and re-injected each turn, so it never
    # needs to be persisted into history (that would just accumulate duplicates).
    save_to_context = False

    def __init__(self, get_index: Callable[[], dict[str, str]]) -> None:
        self._get_index = get_index

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        index = self._get_index() if self._get_index else {}
        if not index:
            return None
        lines = [
            "# Additional tools (search to enable)",
            "These tools exist but are not loaded. Call SearchTools(query=...) with "
            "keywords to reveal the ones you need — their full schema arrives and "
            "they become callable on the next turn.",
        ]
        # Sorted → byte-stable across turns.
        for tool_name in sorted(index):
            lines.append(f"- {tool_name}: {index[tool_name]}")
        return "\n".join(lines)


__all__ = ["DeferredToolIndexContextSource"]
