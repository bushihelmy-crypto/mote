"""Sleep tool — lets the agent pause without consuming LLM inference.

The framework blocks the coroutine until an **event** wakes it: a new message
arrives in the agent's message buffer (user input, background-task
notification, etc.) or a background task completes. There is no duration — the
agent sleeps indefinitely and wakes the instant something happens.

The wake coordination touches Role-owned internals (the message buffer and the
background task pool), so it lives behind the ``wait_interruptible`` Role
capability; this tool stays a thin trigger.
"""
from __future__ import annotations

from typing import ClassVar

from mote.common.utils.report import ArtifactsReporter
from mote.executor.base_tool import BaseTool
from mote.executor.capability_types import WaitInterruptible
from mote.executor.tool_registry import register_tool

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the return site).
_MSG_SLEEPING = "Waiting for an event (new message or background task completion) to wake. You can send a message to wake at any time."
_MSG_WOKE = "Woke after {seconds}s"


@register_tool
class Sleep(BaseTool):
    """Wait until an event wakes you. Wakes the moment a message arrives or a background task completes.

    Use this when you have nothing to do, or when you're waiting for a background
    task — you wake the moment it completes or a message arrives. There is no
    duration; the wait is purely event-driven. Prefer this over polling a task's
    state.

    CRITICAL: This tool MUST be called alone in a single response. Do NOT output
    any other command together with Sleep. Only output the Sleep command and
    nothing else.

    Prefer this over `Terminal.run(sleep ...)` — it doesn't hold a shell process.
    """

    name = "Sleep"
    aliases = ["sleep"]
    # Recall synonyms for tool-search: ways a model asks to wait/pause that the
    # summary ("wait for a duration or a background task") does not spell out.
    keywords: ClassVar[list[str]] = ["pause", "delay", "wait", "hold", "poll", "backoff", "暂停", "等待", "延迟", "后台", "轮询"]
    requires = ("wait_interruptible",)
    # Sleep blocks until an EXTERNAL wake event (a new message or a
    # background-task completion). A foreground graph (run_graph) delivers
    # neither, so a Sleep node would hang the whole run — refuse it as a node.
    graph_excluded: ClassVar[bool] = True
    # Sleep returns a one-line status; cap tiny.
    max_result_size_chars: ClassVar[int] = 1_000

    # Injected from Role by bind(): Role.wait_interruptible.
    wait_interruptible: WaitInterruptible

    async def call(self) -> str:
        """Wait until an event wakes you — a new message or a background task.

        Block until a new message arrives or a background task completes, then
        wake immediately. The wait is purely event-driven — there is no
        duration. Use this when you have nothing to do, or when you're waiting
        for a background task. Prefer this over ``Terminal.run(sleep ...)`` (it
        doesn't hold a shell process) and over polling a task's state.

        **CRITICAL**: Call this tool ALONE — do not combine it with any other
        command in the same response.

        Only wait when no pending work remains. Takes no arguments — just call
        Sleep and you wake the instant something happens.
        """
        async with ArtifactsReporter() as reporter:
            await reporter.async_report(
                {
                    "status": "sleeping",
                    "artifact_type": "sleep",
                    "message": _MSG_SLEEPING,
                },
                "object",
            )

        slept_seconds = await self.wait_interruptible()
        return _MSG_WOKE.format(seconds=slept_seconds)
