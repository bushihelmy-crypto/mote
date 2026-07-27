#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``PromptBroker`` — correlate a blocking port prompt with a back-channel reply.

A conversational port on a *persistent* transport (terminal stdin, Textual
modal) can block a turn on the human inline: the same coroutine that raises the
prompt awaits the answer. AG-UI can't — it is a **stateless request/response**
protocol, so an approval raised while a ``POST /run`` turn streams must be
answered by a *separate* ``POST /respond`` request. The two live in different
handler coroutines, so the blocking side (``AguiPort.decide_approval`` awaiting
a human) and the resolving side (the ``/respond`` handler) need a shared
rendezvous keyed by a minted prompt id.

:class:`PromptBroker` is that rendezvous — one app-scoped instance shared across
all requests. The port :meth:`open`\\s a future under a fresh id, emits the
prompt frame down its SSE stream, and awaits the future; the ``/respond``
handler :meth:`resolve`\\s it with the posted payload. On shutdown /scope
teardown :meth:`cancel_all` fails every pending waiter so no turn hangs forever.

It carries no protocol knowledge (the payload is an opaque dict the port maps to
an ``ApprovalDecision`` / answers) and no external deps — just a dict of
``asyncio.Future``. Single-event-loop safe (all access is on the app's loop).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, Optional

from mote.runtime.logging import logger


class PromptBroker:
    """App-scoped map of ``prompt_id → Future`` bridging blocking prompt / reply."""

    def __init__(self) -> None:
        self._pending: Dict[str, "asyncio.Future[Any]"] = {}

    def new_id(self, prefix: str = "prompt") -> str:
        """Mint a fresh, collision-free correlation id for one prompt."""
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    def open(self, prompt_id: str) -> "asyncio.Future[Any]":
        """Register *prompt_id* and return the future the reply will resolve.

        The caller (the port) awaits the returned future after emitting the
        prompt frame. A duplicate id (should not happen — ids are minted) reuses
        the existing future rather than orphaning a waiter.
        """
        fut = self._pending.get(prompt_id)
        if fut is None or fut.done():
            fut = asyncio.get_event_loop().create_future()
            self._pending[prompt_id] = fut
        return fut

    def resolve(self, prompt_id: str, payload: Any) -> bool:
        """Deliver *payload* to a waiter; return ``True`` iff one was pending.

        The ``/respond`` handler calls this. An unknown / already-resolved id
        (stale retry, wrong thread) returns ``False`` so the transport can reply
        with a clean "no such pending prompt" instead of erroring.
        """
        fut = self._pending.pop(prompt_id, None)
        if fut is None:
            return False
        if not fut.done():
            fut.set_result(payload)
        return True

    def discard(self, prompt_id: str) -> None:
        """Drop a pending entry without resolving (the waiter gave up/timed out)."""
        self._pending.pop(prompt_id, None)

    def cancel_all(self, reason: Optional[str] = None) -> None:
        """Fail every pending waiter (server shutdown / scope teardown).

        Each awaiting prompt raises :class:`asyncio.CancelledError`; the port
        catches it and falls back to its safe default (deny / empty) so a turn
        in flight never hangs on a human who can no longer reply.
        """
        pending, self._pending = self._pending, {}
        for prompt_id, fut in pending.items():
            if not fut.done():
                fut.cancel()
                logger.debug(f"PromptBroker.cancel_all: cancelled pending prompt {prompt_id} ({reason or 'shutdown'})")

    @property
    def pending_ids(self) -> list:
        """The currently-open prompt ids (diagnostics / tests)."""
        return list(self._pending.keys())


__all__ = ["PromptBroker"]
