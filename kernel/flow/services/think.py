"""Model-think operation for the ReAct flow."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from mote.contracts.schema import UserMessage
from mote.kernel.flow.think_checkpoint import ThinkCheckpoint
from mote.kernel.telemetry import span


class ThinkService:
    """Prepare and start one model request behind a recoverable checkpoint."""

    def __init__(
        self,
        *,
        is_active: Callable[[], bool],
        checkpoint: ThinkCheckpoint,
        context_provider: Any,
        think_engine: Any,
        output_engine: Any,
        memory: Any,
        set_channel: Callable[[Any], None],
        turn_context_bus: Any = None,
        get_cwd: Callable[[], str] | None = None,
    ) -> None:
        self._is_active = is_active
        self._checkpoint = checkpoint
        self._context_provider = context_provider
        self._think_engine = think_engine
        self._output_engine = output_engine
        self._memory = memory
        self._set_channel = set_channel
        self._turn_context_bus = turn_context_bus
        self._get_cwd = get_cwd

    async def think(self) -> bool:
        if not self._is_active():
            return False
        if self._checkpoint.reinstate():
            return True
        await self._record_turn_context()
        async with span("think"):
            request = await self._context_provider.prepare()
            model_call_id = self._checkpoint.resume_model_call_id() or uuid4().hex
            route = await self._context_provider.resolve_model_route(
                request,
                model_call_id=model_call_id,
            )
            request = self._context_provider.finalize_for_model(request, route)
            self._set_channel(request.command_channel)
            resume = self._checkpoint.resume_model_call_id() is not None
            if not resume:
                self._checkpoint.begin(model_call_id)
            await self._think_engine.start(
                request.req,
                request.system_prompt,
                tool_specs=request.tool_specs,
                model_route=route,
                model_call_id=model_call_id,
                resume=resume,
                output_binding=request.output_binding.binding,
                output_schema=request.output_schema,
                output_run_id=self._output_engine.run_id,
                schema_fingerprint=request.schema_fingerprint,
            )
        return True

    async def _record_turn_context(self) -> None:
        if self._turn_context_bus is None:
            return
        cwd = self._get_cwd() if self._get_cwd is not None else None
        block = await self._turn_context_bus.collect_to_context(cwd=cwd or None)
        if block:
            await self._memory.add(UserMessage(content=block))


__all__ = ["ThinkService"]
