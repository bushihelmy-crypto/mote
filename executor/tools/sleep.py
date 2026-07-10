"""Sleep tool — lets the agent pause without consuming LLM inference.

Aligned with Claude Code's ``SleepTool``: the agent specifies a
``duration_seconds`` and the framework blocks the coroutine for that long. The
sleep is **interruptible** — if a new message arrives in the agent's message
buffer (user input, background-task notification, etc.) or a background task
completes, the sleep ends early so the agent can react immediately.

The wake coordination touches Role-owned internals (the message buffer and the
background task pool), so it lives behind the ``wait_interruptible`` Role
capability; this tool stays a thin trigger.
"""
from __future__ import annotations

from typing import Awaitable, Callable, ClassVar

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool
from metagpt.common.utils.report import ArtifactsReporter

# Long default (10 min) is fine: the sleep is interruptible, so the agent wakes
# the instant a task completes — no need to guess a duration.
_DEFAULT_SLEEP_SECONDS: float = 600.0


@register_tool
class Sleep(BaseTool):
    """Wait for a specified duration. The user can interrupt the sleep at any time.

    Use this when you have nothing to do, or when you're waiting for a background
    task — you wake the moment it completes or a message arrives. Duration is
    optional (defaults to 10 min); prefer this over polling a task's state.

    CRITICAL: This tool MUST be called alone in a single response. Do NOT output
    any other command together with Sleep. Only output the Sleep command and
    nothing else.

    Prefer this over `Terminal.run(sleep ...)` — it doesn't hold a shell process.
    """

    name = "Sleep"
    aliases = ["sleep"]
    requires = ("wait_interruptible",)
    # Sleep returns a one-line status; cap tiny (CC).
    max_result_size_chars: ClassVar[int] = 1_000

    # Injected from Role by bind(): Role.wait_interruptible.
    wait_interruptible: Callable[[float], Awaitable[tuple[float, bool]]]

    async def call(self, *, duration_seconds: float = _DEFAULT_SLEEP_SECONDS) -> str:
        """Wait for a duration, interruptible by new messages/task completion.

        **CRITICAL**: Call this tool ALONE — do not combine it with any other
        command in the same response.

        Only wait when no pending work remains. Duration is optional (defaults to
        10 min); you wake early on any activity, so just call Sleep with no
        arguments to wait for a background task.

        Args:
            duration_seconds (float): Seconds to sleep. Optional; defaults to 600.
        """
        async with ArtifactsReporter() as reporter:
            await reporter.async_report(
                {
                    "status": "sleeping",
                    "artifact_type": "sleep",
                    "duration_seconds": duration_seconds,
                    "message": (
                        "Waiting for background tasks to complete. You can send a "
                        "message to interrupt at any time."
                    ),
                },
                "object",
            )

        slept_seconds, interrupted = await self.wait_interruptible(duration_seconds)

        if interrupted:
            return f"Sleep interrupted after {slept_seconds}s"
        return f"Slept for {slept_seconds}s"
