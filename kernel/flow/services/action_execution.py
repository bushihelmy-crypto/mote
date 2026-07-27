"""Transactional execution of one model-emitted tool batch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mote.contracts.schema import AIMessage, CauseBy, Message
from mote.kernel.flow.context import FlowContext
from mote.kernel.flow.services.actions import ActionDispatcher
from mote.kernel.parser.channel import join_command_outputs
from mote.kernel.telemetry import span


class ActionExecutionService:
    """Execute and durably record one semantic action batch.

    The service owns the transaction ordering between assistant call recording,
    external effects, result pairing, and think-journal reaping. Graph topology
    only decides when an action batch runs.
    """

    def __init__(
        self,
        *,
        context: Callable[[], FlowContext],
        channel: Callable[[], Any],
        think_engine: Any,
        executor: Any,
        memory: Any,
        report_think_result: Callable[[Any], None],
        complete_think: Callable[[], None],
        reap_think: Callable[[], None],
        set_active: Callable[[bool], None],
        drain_writes: Callable[[], Awaitable[None]],
        dispatcher: ActionDispatcher | None = None,
    ) -> None:
        self._context = context
        self._channel = channel
        self._think_engine = think_engine
        self._executor = executor
        self._memory = memory
        self._report_think_result = report_think_result
        self._complete_think = complete_think
        self._reap_think = reap_think
        self._set_active = set_active
        self._drain_writes = drain_writes
        self._dispatcher = dispatcher or ActionDispatcher()

    async def execute(self, turn=None) -> Message:
        async with span("act"):
            channel = self._channel()
            if turn is None:
                turn = await channel.model_turn(self._think_engine)
            commands = self._dispatcher.tool_commands(turn, set(self._context().tools))
            self._report_think_result(self._think_engine.result)
            content = self._think_engine.result.content
            self._complete_think()

            executed: list[dict] = [
                {
                    "id": command.get("id"),
                    "name": command["command_name"],
                    "args": command.get("args") or {},
                    "output": "",
                    "success": True,
                }
                for command in commands
            ]
            checkpoint = any(
                self._executor.will_ledger(entry["name"], entry["args"], entry["id"]) for entry in executed
            )
            if checkpoint:
                await channel.record_call(self._memory, content, executed)
                await self._drain_writes()

            failed = False
            try:
                for entry in executed:
                    if failed:
                        entry["output"] = (
                            f"[SKIPPED] Command {entry['name']} was not executed because an earlier "
                            "command failed. Please replan in the next round."
                        )
                        entry["success"] = False
                        entry["settled"] = True
                        continue
                    result = await self._executor.run_command(
                        entry["name"],
                        entry["args"],
                        result_id=entry["id"],
                    )
                    self._apply_result(entry, result)
                    failed = not result.success
                    if result.terminate:
                        self._set_active(False)
            except BaseException:
                if checkpoint:
                    for entry in executed:
                        if not entry.get("settled"):
                            entry["output"] = "[INTERRUPTED] Command did not complete (the turn was interrupted)."
                            entry["success"] = False
                    await channel.record_results(self._memory, executed)
                raise

            outputs = join_command_outputs(executed)
            if checkpoint:
                await channel.record_results(self._memory, executed)
            else:
                await channel.record_turn(self._memory, content, executed)
            self._reap_think()
            await self._think_engine.join()
            return AIMessage(
                content=channel.react_result(outputs),
                sent_from=self._context().name,
                cause_by=CauseBy.RUN_COMMAND,
            )

    @staticmethod
    def _apply_result(entry: dict[str, Any], result: Any) -> None:
        entry["output"] = result.output
        entry["success"] = result.success
        media = getattr(result, "media", None)
        if media:
            entry["media"] = media
        for attribute in ("retention", "resource_path"):
            value = getattr(result, attribute, None)
            if value:
                entry[attribute] = value
        if result.data is not None:
            entry["data"] = result.data
        entry["settled"] = True


__all__ = ["ActionExecutionService"]
