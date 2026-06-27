"""BackgroundTaskContextSource — background-task progress as an ephemeral feed.

Implements the ``EphemeralContextSource`` Protocol so the (low-layer)
``TurnContextBus`` can surface running/finishing background tasks each think()
cycle without importing ``tasks``. It wraps :class:`TaskAttachmentGenerator`,
which is designed to be polled once per LLM query: it tracks per-task read
offsets and de-dupes terminal tasks already announced via ``msg_buffer`` (so we
never double-report a completion).

Lives in ``tasks`` (not ``context/turn_context``) precisely so the bus stays
free of any upward import; ``Role`` injects an instance into the bus.

The background pool is created lazily on the Role, so this source takes a
``get_pool`` callable (peek, may return ``None``) and only builds its generator
once a pool exists — keeping the generator's offset/notified state stable across
cycles thereafter.
"""

from __future__ import annotations

from typing import Callable, Optional

from metagpt.executor.tasks.attachment import TaskAttachmentGenerator, format_attachment_xml


class BackgroundTaskContextSource:
    """Renders ``<task-attachment>`` blocks for in-flight background tasks."""

    name = "background_tasks"
    priority = 30
    save_to_context = False  # ephemeral: in-flight task progress, never persisted

    def __init__(self, get_pool: Callable[[], object], store=None) -> None:
        self._get_pool = get_pool
        self._store = store
        self._generator: Optional[TaskAttachmentGenerator] = None

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        pool = self._get_pool()
        if pool is None:
            return None
        if self._generator is None:
            # Build once the pool exists so offset/notified state persists.
            self._generator = TaskAttachmentGenerator(pool, self._store)

        result = await self._generator.generate()
        if not result.attachments:
            return None
        return "\n".join(format_attachment_xml(a) for a in result.attachments)


__all__ = ["BackgroundTaskContextSource"]
