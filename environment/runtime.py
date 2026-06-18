#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AgentRuntime — a live agent: a Role + its asyncio task + status/mailbox.

Port of codex ``CodexThread`` (the live half). An ``AgentRuntime`` wraps one
``Role`` together with:
  * its :class:`Mailbox` (inbound, drained only at turn boundaries),
  * a ``status`` (``AgentStatus``) + ``active_turn`` flag,
  * a per-runtime ``wake_event`` used by the scheduler to start a turn
    (deliberately separate from the message buffer's new-message signal —
    awaited via ``wait_for_message()`` — which is drain-cleared and owned by
    ``Role.wait_interruptible``).

A "turn" is exactly one ``Role.run()``. Mailbox draining is owned by the
scheduler, not the react loop, so deferral of mid-turn mail is free and
``ReActLoop`` stays untouched.

The Role is duck-typed (``session_id``, ``run()``, ``dump()``,
``state.msg_buffer``) so this module never imports ``Role`` — keeping the
``environment -> roles`` dependency lazy and cycle-free.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Optional

from metagpt.common.logs import log_class
from metagpt.environment.agent_path import AgentPath
from metagpt.environment.mailbox import Mailbox


class AgentStatus(str, Enum):
    """Lifecycle status of an agent runtime."""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    ERRORED = "errored"
    INTERRUPTED = "interrupted"
    NOT_FOUND = "not_found"


#: Statuses considered "final" for unloadability (codex ``is_unloadable``).
FINAL_STATUSES = frozenset({AgentStatus.COMPLETED, AgentStatus.ERRORED, AgentStatus.INTERRUPTED})


def is_final(status: AgentStatus) -> bool:
    return status in FINAL_STATUSES


@log_class(
    level="DEBUG",
    exclude={"wake", "session_id", "msg_buffer", "stopped"},
)
class AgentRuntime:
    """A live ``Role`` plus its scheduling state."""

    def __init__(self, role: Any, mailbox: Optional[Mailbox] = None, *, agent_path: Optional[AgentPath] = None):
        self.role = role
        self.mailbox = mailbox if mailbox is not None else Mailbox()
        self.agent_path = agent_path
        self.status: AgentStatus = AgentStatus.IDLE
        self.active_turn: bool = False
        # The exception from the most recent turn that ERRORED (cleared at the
        # start of each turn). The scheduler swallows turn exceptions to keep
        # driving, so this is the only place a consumer (e.g. the REPL) can read
        # back *why* a turn failed and surface it instead of a blank reply.
        self.last_error: Optional[BaseException] = None
        self.wake_event = asyncio.Event()
        self._lock = asyncio.Lock()
        # The scheduler's driver task for this runtime (set by EventDrivenScheduler).
        self.task: Optional[asyncio.Task] = None
        self._stopped = False
        # Wire task-completion wake: when a background task finishes its
        # notification gets pushed to msg_buffer, and this wake ensures the
        # scheduler starts a new turn to process it.
        if hasattr(role, "set_task_completion_wake"):
            role.set_task_completion_wake(self.wake)

    # ------------------------------------------------------------------
    # Identity / duck-typed Role views
    # ------------------------------------------------------------------
    @property
    def session_id(self) -> str:
        return self.role.session_id

    @property
    def msg_buffer(self):
        return self.role.state.msg_buffer

    # ------------------------------------------------------------------
    # Waking
    # ------------------------------------------------------------------
    def wake(self) -> None:
        """Signal the scheduler that this runtime should start a turn."""
        self.wake_event.set()

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------
    async def run_one_turn(self, with_message: Any = None) -> Any:
        """Run exactly one ``Role.run()`` under the runtime lock.

        Sets ``active_turn`` around the call and derives ``status`` from the
        outcome: COMPLETED on a normal return, INTERRUPTED on cancellation,
        ERRORED on any other exception.
        """
        async with self._lock:
            self.active_turn = True
            self.status = AgentStatus.RUNNING
            self.last_error = None
            try:
                if with_message is not None:
                    rsp = await self.role.run(with_message)
                else:
                    rsp = await self.role.run()
                self.status = AgentStatus.COMPLETED
                return rsp
            except asyncio.CancelledError:
                self.status = AgentStatus.INTERRUPTED
                raise
            except Exception as exc:  # noqa: BLE001 — record + surface failure as status
                self.status = AgentStatus.ERRORED
                self.last_error = exc
                raise
            finally:
                self.active_turn = False

    # ------------------------------------------------------------------
    # Unloadability (codex ``is_unloadable``)
    # ------------------------------------------------------------------
    def is_unloadable(self) -> bool:
        """True when the agent can be safely materialized + evicted.

        Requires a final status, no active turn, an empty mailbox, and an empty
        message buffer (no in-flight work would be silently dropped).
        """
        if not is_final(self.status):
            return False
        if self.active_turn:
            return False
        if not self.mailbox.empty():
            return False
        try:
            return self.msg_buffer.empty()
        except Exception:  # noqa: BLE001
            return True

    # ------------------------------------------------------------------
    # Shutdown (codex ``shutdown_and_wait``)
    # ------------------------------------------------------------------
    async def shutdown(self) -> None:
        """Cancel the driver task (if any) and await its completion."""
        self._stopped = True
        self.wake_event.set()  # unblock a driver parked on wake_event
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        self.task = None

    @property
    def stopped(self) -> bool:
        return self._stopped


__all__ = ["AgentStatus", "AgentRuntime", "FINAL_STATUSES", "is_final"]
