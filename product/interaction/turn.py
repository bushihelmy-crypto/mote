"""Canonical execution semantics for one interactive Agent turn."""

from __future__ import annotations

import asyncio
from typing import Any

from mote.product.presentation.events import ErrorRaised, MediaBlock, MessageBlockCompleted
from mote.product.presentation.projection.base import BaseProjector


def format_turn_error(err: BaseException) -> str:
    cls = type(err).__name__
    detail = str(err).strip() or repr(err)
    status = getattr(err, "status_code", None)
    if status is not None:
        return f"{cls} (HTTP {status}): {detail}"
    return f"{cls}: {detail}"


class TurnRunner:
    """Publish input, drive the control plane, and surface terminal errors."""

    def __init__(
        self,
        control: Any,
        agent_id: str,
        projector: BaseProjector,
        *,
        quiescent_poll_interval: float = 0.05,
    ) -> None:
        self._control = control
        self._agent_id = agent_id
        self._projector = projector
        self._quiescent_poll_interval = quiescent_poll_interval

    async def run(self, message: Any, *, media: list[dict[str, Any]] | None = None) -> None:
        await self._projector.deliver(
            MessageBlockCompleted(
                role="user",
                markdown=getattr(message, "content", "") or "",
                streamed=False,
                message_id=getattr(message, "id", None),
            )
        )
        for item in media or []:
            await self._projector.deliver(
                MediaBlock(
                    media_kind="image",
                    ref=item.get("path", "") or "",
                    mime=item.get("mime"),
                    alt=item.get("path", "") or "image",
                )
            )
        self._control.send_input(self._agent_id, message)
        await asyncio.sleep(0)
        while not self._control.quiescent():
            await asyncio.sleep(self._quiescent_poll_interval)
        runtime = self._control.get_runtime(self._agent_id)
        error = getattr(runtime, "last_error", None) if runtime is not None else None
        if error is not None:
            await self._projector.deliver(ErrorRaised(text=format_turn_error(error)))


__all__ = ["TurnRunner", "format_turn_error"]
