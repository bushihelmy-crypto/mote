"""Sleep tool — lets the agent pause without consuming LLM inference.

The framework blocks the coroutine until an **event** wakes it: a new message
arrives in the agent's message buffer (user input, background-task
notification, etc.), or a background task completes. The wait is capped at
``_MAX_WAIT_SECONDS`` as a safety ceiling so a stuck agent can never park
forever, but in practice it wakes the instant something happens.

The ceiling is a **durable timer**: its deadline is journaled so a crash-resume
continues waiting the *remaining* time rather than restarting the countdown.
The wake coordination touches Role-owned internals (the message buffer, the
background task pool, the run journal), so it lives behind the
``wait_interruptible`` Role capability; this tool stays a thin trigger.
"""

from __future__ import annotations

from typing import ClassVar

from mote.contracts.tool.effects import ToolEffect
from mote.runtime.telemetry.reporting import ArtifactsReporter
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import WaitInterruptible

# Safety ceiling for a single wait: the agent wakes on any event long before
# this, but never parks past it (a stuck agent can't hang forever).
_MAX_WAIT_SECONDS = 3600.0

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the return site).
_MSG_SLEEPING = "Waiting for an event (new message or background task completion) to wake. You can send a message to wake at any time."
_MSG_WOKE = "Woke after {seconds}s"


class Sleep(BaseTool):
    """Wait until an event wakes you — the moment a message arrives or a background task completes.

    Use this when you have nothing to do, or when you're waiting for a
    background task — you wake the instant it completes or a message arrives.
    Prefer this over polling a task's state.

    CRITICAL: This tool MUST be called alone in a single response. Do NOT output
    any other command together with Sleep. Only output the Sleep command and
    nothing else.
    """

    name = "Sleep"
    aliases = ["sleep"]
    # Recall synonyms for tool-search: ways a model asks to wait/pause that the
    # summary ("wait until an event wakes you") does not spell out.
    keywords: ClassVar[list[str]] = [
        "pause",
        "delay",
        "wait",
        "hold",
        "poll",
        "backoff",
        "暂停",
        "等待",
        "延迟",
        "后台",
        "轮询",
    ]
    requires = ("wait_interruptible",)
    # Pure wait — blocks on an external wake event but produces NO side effect.
    # Declare PURE so it opts out of the effect ledger and, on a crash-resume,
    # reconciles as replay-safe (`<not-executed>`) rather than being mistaken for
    # an EXTERNAL effect (`<unknown-after-crash>`), which the untagged default
    # would wrongly infer.
    effect: ClassVar[ToolEffect] = ToolEffect.PURE
    # Sleep blocks until an EXTERNAL wake event (a new message or a
    # background-task completion). A foreground orchestration delivers neither,
    # so a Sleep node would hang the whole run — refuse it as a node.
    graph_excluded: ClassVar[bool] = True
    # Sleep returns a one-line status; cap tiny.
    max_result_size_chars: ClassVar[int] = 1_000

    # Injected from Role by bind(): Role.wait_interruptible.
    wait_interruptible: WaitInterruptible

    async def call(self) -> str:
        """Wait until an event wakes you — a background task or a user message.

        Block until a new user message arrives or a background task completes,
        then wake immediately. Use it when idle or waiting for a background task;
        prefer it over polling a task's state.

        **CRITICAL**: Call this tool ALONE — do not combine it with any other
        command in the same response.
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

        slept_seconds = await self.wait_interruptible(_MAX_WAIT_SECONDS)
        return _MSG_WOKE.format(seconds=slept_seconds)
