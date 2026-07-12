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
    """Orders and merges turn-context sources into ``<system-reminder>`` blocks.

    Sources fall into two disjoint buckets keyed by their ``save_to_context``
    flag (default ``True``):

    - ``collect_to_context`` renders the ``save_to_context=True`` sources — the
      block the Role persists into history once per turn.
    - ``collect`` renders the ``save_to_context=False`` sources — the ephemeral,
      request-only block appended to the cycle's user prompt.

    Both share the same concurrent-render / priority-order / merge machinery.
    """

    def __init__(self, sources: Sequence[EphemeralContextSource]) -> None:
        # Stable priority order (lower first); ties keep registration order.
        self._sources: List[EphemeralContextSource] = sorted(
            sources, key=lambda s: getattr(s, "priority", 0)
        )
        # Partition by save_to_context (missing attribute => persisted).
        self._persistent: List[EphemeralContextSource] = [
            s for s in self._sources if getattr(s, "save_to_context", True)
        ]
        self._ephemeral: List[EphemeralContextSource] = [
            s for s in self._sources if not getattr(s, "save_to_context", True)
        ]

    async def collect(self, *, cwd: Optional[str] = None) -> str:
        """Render the ephemeral (request-only) sources into one reminder block.

        These are the ``save_to_context=False`` feeds; their block is appended to
        the cycle's user prompt and never stored in history. Returns ``""`` when
        nothing was produced.
        """
        return await self._render_bucket(self._ephemeral, cwd)

    async def collect_to_context(self, *, cwd: Optional[str] = None) -> str:
        """Render the persisted sources into one reminder block for history.

        These are the ``save_to_context=True`` feeds (the default); the Role
        writes their block into history through the ``ContextManager`` once per
        turn. Returns ``""`` when nothing was produced.
        """
        return await self._render_bucket(self._persistent, cwd)

    async def _render_bucket(
        self, sources: List[EphemeralContextSource], cwd: Optional[str]
    ) -> str:
        """Render a bucket of sources concurrently and merge the survivors.

        Returns ``""`` when the bucket is empty or no source produced anything.
        An exception (or non-string return) from one source is logged and
        dropped, never propagated.
        """
        if not sources:
            return ""

        results = await asyncio.gather(
            *(self._render_one(s, cwd) for s in sources)
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
