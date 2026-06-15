"""TurnContextBus — aggregates per-turn ephemeral context sources.

The single object ``PromptBuilder._make_reminders`` calls each think() cycle. It
holds a list of :class:`EphemeralContextSource` (git status, token-pressure,
background-task progress, LSP diagnostics, ...), renders them concurrently,
orders the non-empty blocks by ``priority``, and wraps them into one
``<system-reminder>`` envelope appended to the cycle's user prompt.

Layering: this lives in the low ``context`` layer and depends only on
``common`` + the injected sources (via the ``EphemeralContextSource`` Protocol).
Sources that need higher layers (``tasks`` for background-task progress) live in
those layers and are injected by ``Role``; the bus never imports them.

Best-effort throughout: each source is guarded independently so one misbehaving
feed can never break a turn, and ``collect`` never raises.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Sequence

from metagpt.common.interface import EphemeralContextSource
from metagpt.common.logs import logger
from metagpt.context.turn_context.format import wrap_system_reminder


class TurnContextBus:
    """Orders and merges ephemeral context sources into one reminder block."""

    def __init__(self, sources: Sequence[EphemeralContextSource]) -> None:
        # Stable priority order (lower first); ties keep registration order.
        self._sources: List[EphemeralContextSource] = sorted(
            sources, key=lambda s: getattr(s, "priority", 0)
        )

    async def collect(self, *, cwd: Optional[str] = None) -> str:
        """Render every source and return the merged ``<system-reminder>`` block.

        Returns ``""`` when no source produced anything. Sources run
        concurrently; an exception (or non-string return) from one is logged and
        dropped, never propagated.
        """
        if not self._sources:
            return ""

        results = await asyncio.gather(
            *(self._render_one(s, cwd) for s in self._sources)
        )
        return wrap_system_reminder(r for r in results if r)

    @staticmethod
    async def _render_one(
        source: EphemeralContextSource, cwd: Optional[str]
    ) -> Optional[str]:
        """Guarded single-source render — never raises into ``collect``."""
        try:
            block = await source.render(cwd=cwd)
        except Exception as exc:  # noqa: BLE001 — one feed must not break the turn
            logger.warning(
                f"turn_context: source {getattr(source, 'name', source)!r} failed: {exc}"
            )
            return None
        if not isinstance(block, str):
            return None
        return block


__all__ = ["TurnContextBus"]
