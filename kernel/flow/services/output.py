"""Typed output validation, transcript recording, and commit service."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from mote.contracts.schema import AIMessage, CauseBy, Message
from mote.kernel.flow.context import FlowContext
from mote.kernel.flow.result import FlowResult


class FlowOutputService:
    """Own the accepted-output transaction and correction recording."""

    def __init__(
        self,
        *,
        context: Callable[[], FlowContext],
        channel: Callable[[], Any],
        think_engine: Any,
        memory: Any,
        output_engine: Any,
        report_think_result: Callable[[Any], None],
        complete_think: Callable[[], None],
        reap_think: Callable[[], None],
        drain_writes: Callable[[], Awaitable[None]],
    ) -> None:
        self._context = context
        self._channel = channel
        self._think_engine = think_engine
        self._memory = memory
        self._output_engine = output_engine
        self._report_think_result = report_think_result
        self._complete_think = complete_think
        self._reap_think = reap_think
        self._drain_writes = drain_writes

    async def evaluate(self, candidate):
        return await self._output_engine.evaluate(candidate)

    async def commit(self):
        return await self._output_engine.commit()

    async def accept(self, candidate) -> Message:
        content = self._think_engine.result.content or ""
        self._report_think_result(self._think_engine.result)
        self._complete_think()
        await self._channel().record_output_candidate(
            self._memory,
            content,
            candidate,
            accepted=True,
        )
        # Acceptance + transcript are the recovery frontier. They must reach
        # durable storage before the think checkpoint is discarded; otherwise
        # a crash here would lose the accepted output and re-pay the model.
        await self._drain_writes()
        self._reap_think()
        await self._think_engine.join()
        response = AIMessage(
            content=content,
            sent_from=self._context().name,
            cause_by=CauseBy.RUN_COMMAND,
        )
        return response

    async def reject(self, evaluation, candidate) -> None:
        content = self._think_engine.result.content or ""
        self._report_think_result(self._think_engine.result)
        self._complete_think()
        feedback = evaluation.feedback() if evaluation.correction_allowed else None
        await self._channel().record_output_candidate(
            self._memory,
            content,
            candidate,
            accepted=False,
            feedback=feedback,
        )
        # Persist the rejected candidate and correction feedback before closing
        # the think window, so resume cannot silently forget a consumed attempt.
        await self._drain_writes()
        self._reap_think()
        await self._think_engine.join()

    async def restore(self) -> FlowResult[Any] | None:
        if not self._output_engine.has_restored_terminal_output:
            return None
        messages = self._memory.get()
        response = next((message for message in reversed(messages) if isinstance(message, AIMessage)), None)
        if response is None:
            encoded = self._output_engine.contract.decoder.encode(self._output_engine.accepted_value)
            content = (
                encoded
                if isinstance(encoded, str)
                else json.dumps(encoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            response = AIMessage(
                content=content,
                sent_from=self._context().name,
                cause_by=CauseBy.RUN_COMMAND,
            )
            await self._memory.add(response)
        committed = await self._output_engine.commit()
        return FlowResult(presentation=response, committed_output=committed)


__all__ = ["FlowOutputService"]
