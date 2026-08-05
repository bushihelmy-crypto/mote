"""Canonical execution semantics for one interactive Agent turn."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from mote.contracts.conversation import Message
from mote.contracts.output import RunResult
from mote.orchestration.agents.control import AgentControl
from mote.product.presentation.events import ErrorRaised, MediaBlock, MessageBlockCompleted, UserMediaIdentity
from mote.product.presentation.projection.base import BaseProjector


@runtime_checkable
class HttpStatusError(Protocol):
    status_code: int


def format_turn_error(err: BaseException) -> str:
    cls = type(err).__name__
    detail = str(err).strip() or repr(err)
    if isinstance(err, HttpStatusError):
        return f"{cls} (HTTP {err.status_code}): {detail}"
    return f"{cls}: {detail}"


class TurnRunner:
    """Publish input, drive the control plane, and surface terminal errors."""

    def __init__(
        self,
        control: AgentControl,
        agent_id: str,
        projector: BaseProjector,
        *,
        quiescent_poll_interval: float = 0.05,
    ) -> None:
        self._control = control
        self._agent_id = agent_id
        self._projector = projector
        self._quiescent_poll_interval = quiescent_poll_interval

    async def run(self, message: Message, *, media: list[dict[str, Any]] | None = None) -> None:
        await self._projector.deliver(
            MessageBlockCompleted(
                role="user",
                markdown=message.content or "",
                streamed=False,
                message_id=message.id,
            )
        )
        for ordinal, item in enumerate(media or [], start=1):
            await self._projector.deliver(
                MediaBlock(
                    identity=UserMediaIdentity(message.id, ordinal),
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
        error = runtime.last_error if runtime is not None else None
        if error is not None:
            await self._projector.deliver(ErrorRaised(text=format_turn_error(error)))
            return
        outcome = runtime.last_run_result if runtime is not None else None
        if isinstance(outcome, RunResult) and isinstance(outcome.output, str):
            await self._projector.deliver(
                MessageBlockCompleted(
                    role="assistant",
                    markdown=outcome.output,
                    streamed=False,
                    message_id=outcome.transcript.terminal_message_id,
                )
            )


__all__ = ["TurnRunner", "format_turn_error"]
